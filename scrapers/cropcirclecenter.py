"""
scrapers/cropcirclecenter.py — Scrape cropcirclecenter.com.

CCC is the descendant of Müller's "International Crop Circle Archive" — the
academic reference, with deterministic URLs:

    date/YYYY/YYYYMM.html                       monthly listing
    ccdata/YYYY/MM/DD/<canonical_id>.html       formation detail
    ccdata/YYYY/MM/DD/<canonical_id>_G.jpg      ground image

Canonical IDs follow CC<YYYYMMDD>_<letter>, e.g. UK20260404_A. We adopt
these as the project's canonical identifiers.

Licensing: CCC images are "all rights reserved" — we record source URLs but
set can_redistribute=false; RLS hides them from the public anon key.

Geocoding: pages have country + county + landmark prose but no GPS. We map
to county centroids (UK) or country centroids (rest), and record
location_precision_m so the public site can render approximate dots
distinctly from EXIF-exact ones.

CLI:
    .venv/bin/python -m scrapers.cropcirclecenter --year 2008 --month 8
    .venv/bin/python -m scrapers.cropcirclecenter --year 2008
    .venv/bin/python -m scrapers.cropcirclecenter --start 1990 --end 2027
    .venv/bin/python -m scrapers.cropcirclecenter --dry-run --year 2008 --month 8
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import warnings

from bs4 import XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from datetime import date as Date
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
import structlog
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (  # noqa: E402
    already_scraped,
    db,
    get_formation_by_canonical_id,
    get_source_id,
    insert_formation,
    link_alias,
    upsert_source_record,
)
from lib.centroids import (  # noqa: E402
    country_to_iso2,
    lookup_country,
    lookup_uk_county,
)

log = structlog.get_logger("ccc")

BASE = "http://www.cropcirclecenter.com"
USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)
SLUG = "cropcirclecenter"
THROTTLE_SECS = 0.6  # ~1.5 req/sec, polite for this small private site
ENCODING = "iso-8859-1"

CANONICAL_RE = re.compile(
    r"ccdata/(\d{4})/(\d{2})/(\d{2})/([A-Z]{2}\d{8}_[A-Z])\.html",
)
DATE_PROSE_RE = re.compile(
    r"(\d{4})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2})",
    re.IGNORECASE,
)
SIZE_M_RE = re.compile(r"(\d{2,4}(?:\.\d+)?)\s*m\b")
SIZE_FT_RE = re.compile(r"(\d{2,4}(?:\.\d+)?)\s*(?:ft|feet|')\b")

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}


# ============================================================================
# HTTP
# ============================================================================
def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        follow_redirects=True,
    )


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)
def fetch(client: httpx.Client, url: str) -> tuple[int, str]:
    r = client.get(url)
    r.raise_for_status()
    # Pages declare iso-8859-1 but actually serve UTF-8 byte sequences.
    # Try UTF-8 first; fall back to latin-1 if that fails.
    try:
        return r.status_code, r.content.decode("utf-8")
    except UnicodeDecodeError:
        return r.status_code, r.content.decode(ENCODING, errors="replace")


# ============================================================================
# Listing iteration
# ============================================================================
def iter_listing_urls(start_year: int, end_year: int) -> Iterator[str]:
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            yield f"{BASE}/date/{year}/{year}{month:02d}.html"


def extract_canonical_links(html: str) -> list[tuple[str, str]]:
    """Return [(canonical_id, full_url), ...] from a listing page."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in CANONICAL_RE.finditer(html):
        cid = m.group(4)
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, f"{BASE}/{m.group(0)}"))
    return out


# ============================================================================
# Detail parsing
# ============================================================================
def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    # Strip nbsp + collapse all whitespace, then trim leading punctuation/junk
    s = s.replace("\xa0", " ").replace("​", "")
    s = re.sub(r"\s+", " ", s).strip(" |,;:-")
    return s or None


def _td_text_after_img(soup: BeautifulSoup, img_filename_pattern: str) -> Optional[str]:
    """Find a td containing img with src ending in pattern; return td text."""
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if src.endswith(img_filename_pattern) or img_filename_pattern in src:
            td = img.find_parent("td")
            if td:
                return _clean(td.get_text(" ", strip=True))
    return None


def _flag_country_text(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    """Find a FLAGS/<cc>.png img; return (country_code, country_text)."""
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        m = re.search(r"FLAGS/([a-z]{2,3})\.png", src, re.IGNORECASE)
        if m:
            cc = m.group(1).upper()
            td = img.find_parent("td")
            txt = td.get_text(" ", strip=True) if td else None
            return cc, (txt or None)
    return None, None


def parse_detail(html: str, canonical_id: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")

    loc_text = _td_text_after_img(soup, "PIX/loc.png")
    landmark, county = None, None
    if loc_text:
        parts = [_clean(p) for p in loc_text.split("|")]
        parts = [p for p in parts if p]
        if parts:
            landmark = parts[0]
            county = parts[1] if len(parts) > 1 else None

    cc_from_flag, country_text = _flag_country_text(soup)
    country_text = _clean(country_text)
    country_iso2 = (
        country_to_iso2(country_text)
        or (cc_from_flag.upper() if cc_from_flag and len(cc_from_flag) == 2 else None)
    )

    day_text = _td_text_after_img(soup, "PIX/day.png")
    event_date: Optional[Date] = None
    date_uncertainty: Optional[str] = None
    if day_text:
        m = DATE_PROSE_RE.search(day_text)
        if m:
            year = int(m.group(1))
            month = MONTHS.get(m.group(2).lower())
            day = int(m.group(3))
            if month:
                try:
                    event_date = Date(year, month, day)
                except ValueError:
                    date_uncertainty = day_text
        else:
            date_uncertainty = day_text

    # Fallback: derive date from canonical_id (CCYYYYMMDD_X)
    if event_date is None:
        m = re.match(r"^[A-Z]{2}(\d{4})(\d{2})(\d{2})_", canonical_id)
        if m:
            try:
                event_date = Date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

    crop_type = _clean(_td_text_after_img(soup, "PIX/type.png"))
    if crop_type:
        crop_type = crop_type.lower()

    size_text = _td_text_after_img(soup, "PIX/size.png")
    diameter_m: Optional[float] = None
    if size_text:
        m = SIZE_M_RE.search(size_text)
        if m:
            diameter_m = float(m.group(1))
        else:
            m = SIZE_FT_RE.search(size_text)
            if m:
                diameter_m = round(float(m.group(1)) * 0.3048, 1)

    note_text = _td_text_after_img(soup, "PIX/note.png")
    notes_clean = (note_text or "").strip() or None

    # Image: <canonical_id>_G.jpg (or _A_G.jpg variants) — same dir as detail page
    image_relname = f"{canonical_id}_G.jpg"
    image_present = any(
        (img.get("src") or "").endswith(image_relname)
        for img in soup.find_all("img")
    )

    return {
        "canonical_id": canonical_id,
        "landmark": landmark,
        "county": county,
        "country_text": country_text,
        "country_iso2": country_iso2,
        "event_date": event_date,
        "date_uncertainty": date_uncertainty,
        "crop_type": crop_type,
        "diameter_m": diameter_m,
        "notes": notes_clean,
        "image_relname": image_relname if image_present else None,
    }


def derive_geo(
    country_iso2: Optional[str], county: Optional[str]
) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """Return (lat, lng, precision_m). UK county centroid > country centroid."""
    if country_iso2 in ("GB", "UK"):
        hit = lookup_uk_county(county)
        if hit:
            return hit
    hit = lookup_country(country_iso2)
    if hit:
        return hit
    return None, None, None


# ============================================================================
# Per-formation processing
# ============================================================================
def process_formation(
    client: httpx.Client,
    canonical_id: str,
    detail_url: str,
    dry_run: bool = False,
) -> str:
    if not dry_run and already_scraped(SLUG, canonical_id):
        return "skip"

    try:
        status, html = fetch(client, detail_url)
        time.sleep(THROTTLE_SECS)
    except Exception as e:
        log.error("fetch.failed", url=detail_url, error=str(e))
        return "failed"

    parsed = parse_detail(html, canonical_id)
    lat, lng, precision_m = derive_geo(parsed["country_iso2"], parsed["county"])

    if dry_run:
        log.info(
            "dry.parse",
            canonical_id=canonical_id,
            country=parsed["country_iso2"],
            county=parsed["county"],
            landmark=parsed["landmark"],
            date=parsed["event_date"].isoformat() if parsed["event_date"] else parsed["date_uncertainty"],
            crop=parsed["crop_type"],
            diameter_m=parsed["diameter_m"],
            lat=lat,
            lng=lng,
            precision_m=precision_m,
            has_image=bool(parsed["image_relname"]),
        )
        return "skip"

    # Side effects in idempotent order; source_record is the "done" marker.
    fid = get_formation_by_canonical_id(canonical_id)
    if not fid:
        fid = insert_formation(
            canonical_id=canonical_id,
            event_date=parsed["event_date"],
            country=parsed["country_iso2"],
            county=parsed["county"],
            nearest_landmark=parsed["landmark"],
            lat=lat,
            lng=lng,
            crop_type=parsed["crop_type"],
            diameter_m=parsed["diameter_m"],
            notes=parsed["notes"] or parsed["date_uncertainty"],
        )
        if precision_m is not None:
            db.table("formations").update({"location_precision_m": precision_m}).eq(
                "id", str(fid)
            ).execute()

    link_alias(fid, SLUG, canonical_id, source_url=detail_url, is_primary=True)

    if parsed["image_relname"]:
        image_url = detail_url.rsplit("/", 1)[0] + "/" + parsed["image_relname"]
        existing = (
            db.table("formation_images")
            .select("id")
            .eq("source_url", image_url)
            .limit(1)
            .execute()
            .data
        )
        if not existing:
            db.table("formation_images").insert(
                {
                    "formation_id": str(fid),
                    "source_id": str(get_source_id(SLUG)),
                    "source_url": image_url,
                    "image_kind": "aerial",  # CCC images are typically aerial
                    "license": "All rights reserved (cropcirclecenter.com)",
                    "license_notes": detail_url,
                    "can_redistribute": False,
                }
            ).execute()

    upsert_source_record(
        source_slug=SLUG,
        source_record_id=canonical_id,
        source_url=detail_url,
        raw_html=html,
        http_status=status,
    )

    return "inserted"


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="cropcirclecenter.com scraper.")
    ap.add_argument("--start", type=int, default=1990, help="Start year (inclusive).")
    ap.add_argument("--end", type=int, default=Date.today().year, help="End year (inclusive).")
    ap.add_argument("--year", type=int, default=None, help="Single year shortcut.")
    ap.add_argument("--month", type=int, default=None, help="Single month (with --year).")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N formations.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    counts = {"inserted": 0, "skip": 0, "failed": 0}
    seen = 0

    if args.year is not None:
        start = end = args.year
    else:
        start, end = args.start, args.end

    with make_client() as client:
        for listing_url in iter_listing_urls(start, end):
            if args.month and not listing_url.endswith(f"{args.year}{args.month:02d}.html"):
                continue
            try:
                status, html = fetch(client, listing_url)
                time.sleep(THROTTLE_SECS)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    continue  # month with no archive page
                log.error("listing.failed", url=listing_url, error=str(e))
                continue
            except Exception as e:
                log.error("listing.failed", url=listing_url, error=str(e))
                continue

            links = extract_canonical_links(html)
            log.info("listing.scan", url=listing_url, found=len(links))

            for cid, durl in links:
                seen += 1
                try:
                    res = process_formation(client, cid, durl, dry_run=args.dry_run)
                    counts[res] += 1
                except Exception as e:
                    log.exception("formation.failed", id=cid, error=str(e))
                    counts["failed"] += 1
                if args.limit and seen >= args.limit:
                    break

            if args.limit and seen >= args.limit:
                break

    print(f"\nDone. Seen {seen} formations.")
    for k, v in counts.items():
        print(f"  {k:10s} {v}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
