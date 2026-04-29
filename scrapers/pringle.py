"""
scrapers/pringle.py — Scrape Lucy Pringle's legacy crop circle photo archive.

Lucy Pringle is a renowned UK crop circle photographer. Her legacy archive
(formations 1990-2014) lives at:

    https://cropcirclephotographs.co.uk/photos/<year>/<month>.shtml

with deterministic month listing pages and per-formation detail pages whose
slug varies by year:

    photos/<year>/<month>.shtml             monthly listing
    photos/<year>/<canonical_id>.shtml      detail (1995-2008, e.g. uk2003be)
    photos/<year>/<descriptive-slug>.shtml  detail (2009-2014, e.g.
                                            wilton-windmill-2010)

Detail pages with `<div class="ccdb">CCDB Ref. <a href="...?k=ID">ID</a></div>`
expose the same Müller canonical ID family used by cropcirclecenter.com
(uk1995am, uk2003be, etc.). We adopt those IDs as canonical so Pringle and
CCC records merge automatically. Pages without a CCDB Ref get a synthetic
`PRINGLE-<slug>` canonical ID.

EXIF GPS check (recon, 2026-04-29): zero EXIF tags on every sampled image
(2003, 2008, 2010, 2013, 2014, 2026). All images are flat JFIF "Save for
Web" exports — Lucy strips EXIF systematically. We therefore record image
URLs and rely on county/landmark prose for geocoding (county centroid),
same as CCC. The Pringle scrape's value is (a) a second-source landmark
prose for cross-checking CCC, (b) more images, (c) the ~150 mid-2000s
formations CCC may not cover.

Licensing: "All rights reserved" — copyright © Lucy Pringle. We record
image URLs only (can_redistribute=false). Site has no robots.txt
disallows; we throttle to 1 req/sec to be respectful.

CLI:
    .venv/bin/python -m scrapers.pringle --dry-run --limit 5
    .venv/bin/python -m scrapers.pringle --year 2003
    .venv/bin/python -m scrapers.pringle --start 1995 --end 2008
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date as Date
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
import structlog
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv(override=True)

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

log = structlog.get_logger("pringle")

BASE = "https://cropcirclephotographs.co.uk"
USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)
SLUG = "pringle"
THROTTLE_SECS = 1.1  # ~0.9 req/sec — respectful, this is a private site

# Crop-circle season — most months outside this range 404. We try them all
# anyway but ignore 404s.
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec"]

# Müller-style canonical ID, e.g. uk2003be, de2007aa
CANONICAL_RE = re.compile(r"^[a-z]{2}\d{4}[a-z]+$")
# CCDB Ref link in detail pages
CCDB_LINK_RE = re.compile(
    r'class="ccdb"[^>]*>\s*CCDB\s+Ref\.?\s*<a[^>]*\?[^"]*?k=([a-zA-Z0-9_-]+)',
    re.IGNORECASE | re.DOTALL,
)
# Date prose: "5th Jul 2003", "Reported 22nd May 2010", "16th April, 2014"
DATE_PROSE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?,?\s+"
    r"(\d{4})",
    re.IGNORECASE,
)
SIZE_M_RE = re.compile(r"(\d{2,4}(?:\.\d+)?)\s*m\b")
SIZE_FT_RE = re.compile(
    r"(\d{2,4}(?:\.\d+)?)\s*(?:ft|feet|')",
    re.IGNORECASE,
)

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"],
        start=1,
    )
}
MONTHS.update({
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
})


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
    return r.status_code, r.text


# ============================================================================
# Listing iteration
# ============================================================================
def iter_listing_urls(start_year: int, end_year: int) -> Iterator[str]:
    for year in range(start_year, end_year + 1):
        for m in MONTH_NAMES:
            yield f"{BASE}/photos/{year}/{m}.shtml"


def extract_detail_links(listing_url: str, html: str) -> list[tuple[str, str]]:
    """
    Return [(canonical_id, full_url), ...] from a monthly listing page.

    canonical_id is the Müller form (uk2003be) when the slug matches that
    pattern, otherwise PRINGLE-<slug> for descriptive 2009+ slugs.
    """
    soup = BeautifulSoup(html, "lxml")
    base_dir = listing_url.rsplit("/", 1)[0]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    month_files = {f"{m}.shtml" for m in MONTH_NAMES}

    for a in soup.find_all("a"):
        href = a.get("href", "") or ""
        if not href.endswith(".shtml"):
            continue
        if href.startswith("..") or href.startswith("/") or "://" in href:
            continue
        bare = href.split("#", 1)[0]
        if bare in month_files or bare == "":
            continue
        if bare in seen:
            continue
        seen.add(bare)
        slug = bare[:-len(".shtml")]
        if CANONICAL_RE.match(slug):
            cid = slug  # Müller-style; keep as-is for cross-source merge
        else:
            cid = f"PRINGLE-{slug}"
        out.append((cid, f"{base_dir}/{bare}"))
    return out


# ============================================================================
# Detail parsing
# ============================================================================
def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.replace("\xa0", " ").replace("​", "")
    s = re.sub(r"\s+", " ", s).strip(" |,;:-")
    return s or None


def _div_text(soup: BeautifulSoup, class_name: str) -> Optional[str]:
    div = soup.find("div", class_=class_name)
    if not div:
        return None
    return _clean(div.get_text(" ", strip=True))


def _split_landmark_county(heading: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Headings look like:
        "A34 Beacon Hill - nr Litchfield, Hampshire"
        "Wilton Windmill, Wiltshire"
        "Cow Down, nr East Kennett, Wiltshire, United Kingdom."
        "Yarnbury Castle, Nr Winterbourne Stoke - Wiltshire"
        "Silbury Hill, nr Avebury - Wiltshire"
    The county is the last token, possibly hyphen-separated from landmark
    prose like "nr <village> - <county>". We split on commas first; if the
    last comma-part still contains a " - ", split on that.
    """
    if not heading:
        return None, None
    parts = [p.strip() for p in heading.split(",")]
    parts = [p.rstrip(".") for p in parts if p.strip()]
    if not parts:
        return None, None

    # Strip a trailing "United Kingdom" / "UK"
    while parts and parts[-1].lower() in ("united kingdom", "uk", "england", "great britain"):
        parts.pop()

    if not parts:
        return heading, None
    if len(parts) == 1:
        # "Yarnbury Castle - Wiltshire" with no comma — try " - " split
        sole = parts[0]
        if " - " in sole:
            left, right = sole.rsplit(" - ", 1)
            if lookup_uk_county(right.strip()):
                return left.strip() or None, right.strip() or None
        return sole, None

    # Last part is most likely the county. If it contains " - " (e.g.
    # "Nr Winterbourne Stoke - Wiltshire"), the right side is the county
    # and the left side belongs to the landmark prose.
    last = parts[-1]
    if " - " in last:
        left, right = last.rsplit(" - ", 1)
        county_candidate = right.strip()
        if lookup_uk_county(county_candidate):
            landmark_parts = parts[:-1] + [left.strip()]
            landmark = ", ".join(p for p in landmark_parts if p)
            return landmark or None, county_candidate or None

    county = last
    landmark = ", ".join(parts[:-1])
    return landmark or None, county or None


def parse_detail(html: str, fallback_canonical: str) -> dict[str, Any]:
    """
    Parse a Pringle detail page. Returns a dict of fields plus a possibly
    updated canonical_id (CCDB Ref overrides the URL-derived guess).
    """
    soup = BeautifulSoup(html, "lxml")

    # CCDB Ref — authoritative canonical
    canonical_id = fallback_canonical
    m = CCDB_LINK_RE.search(html)
    if m:
        canonical_id = m.group(1).strip()
    else:
        # Fallback: bare div.ccdb text "CCDB Ref. uk1995am"
        ccdb = _div_text(soup, "ccdb")
        if ccdb:
            mm = re.search(r"CCDB\s+Ref\.?\s*([a-zA-Z0-9_-]+)", ccdb, re.IGNORECASE)
            if mm and CANONICAL_RE.match(mm.group(1).lower()):
                canonical_id = mm.group(1).lower()

    heading = _div_text(soup, "heading")
    landmark, county = _split_landmark_county(heading)

    # Country: Pringle archive is overwhelmingly UK; if the heading mentions
    # a non-UK country we'd defer to that, but in practice the legacy
    # archive is UK-only. We default GB and let country_to_iso2 catch
    # anything explicit.
    country_iso2: Optional[str] = "GB"
    if heading:
        # A heading like "Some Place, Germany" should win over default GB
        last_part = heading.rsplit(",", 1)[-1].strip().rstrip(".")
        cc = country_to_iso2(last_part)
        if cc:
            country_iso2 = cc
            # And remove that from county
            if county and country_to_iso2(county):
                # county WAS the country; recompute
                parts = [p.strip().rstrip(".") for p in heading.split(",") if p.strip()]
                parts = [p for p in parts if not country_to_iso2(p)]
                if len(parts) >= 2:
                    landmark, county = ", ".join(parts[:-1]), parts[-1]
                elif parts:
                    landmark, county = parts[0], None

    date_text = _div_text(soup, "date")
    event_date: Optional[Date] = None
    date_uncertainty: Optional[str] = None
    if date_text:
        mm = DATE_PROSE_RE.search(date_text)
        if mm:
            day = int(mm.group(1))
            month = MONTHS.get(mm.group(2).lower())
            year = int(mm.group(3))
            if month:
                try:
                    event_date = Date(year, month, day)
                except ValueError:
                    date_uncertainty = date_text
            else:
                date_uncertainty = date_text
        else:
            date_uncertainty = date_text

    # Fallback: derive year + month from the URL-style canonical (uk2003be → 2003 only)
    # and from the surrounding listing URL handled at the call site if needed.

    desc_div = soup.find("div", class_="desc")
    notes = _clean(desc_div.get_text(" ", strip=True)) if desc_div else None

    # Size — sometimes embedded in desc prose (e.g. "c.120 ft overall")
    diameter_m: Optional[float] = None
    if notes:
        mm_m = SIZE_M_RE.search(notes)
        if mm_m:
            diameter_m = float(mm_m.group(1))
        else:
            mm_ft = SIZE_FT_RE.search(notes)
            if mm_ft:
                diameter_m = round(float(mm_ft.group(1)) * 0.3048, 1)

    # Images: full-size <img> tags inside <center> (or anywhere) whose src is
    # a same-directory .jpg, ignoring banner / wp-content / sm thumbnails.
    images: list[str] = []
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src.lower().endswith(".jpg"):
            continue
        if "banner" in src or "wp-content" in src or "title" in src.lower():
            continue
        # Skip thumbnails (CCC-style "sm" suffix is rare here, but be safe)
        base = src.rsplit("/", 1)[-1].lower()
        if base.endswith("sm.jpg"):
            continue
        if src not in images:
            images.append(src)

    return {
        "canonical_id": canonical_id,
        "landmark": landmark,
        "county": county,
        "country_iso2": country_iso2,
        "event_date": event_date,
        "date_uncertainty": date_uncertainty,
        "crop_type": None,  # Pringle pages rarely state crop type
        "diameter_m": diameter_m,
        "notes": notes,
        "image_relnames": images,
    }


def derive_geo(
    country_iso2: Optional[str], county: Optional[str]
) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """Same precedence as CCC: UK county centroid > country centroid."""
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
    initial_canonical: str,
    detail_url: str,
    dry_run: bool = False,
) -> str:
    if not dry_run and already_scraped(SLUG, initial_canonical):
        return "skip"

    try:
        status, html = fetch(client, detail_url)
        time.sleep(THROTTLE_SECS)
    except Exception as e:
        log.error("fetch.failed", url=detail_url, error=str(e))
        return "failed"

    parsed = parse_detail(html, initial_canonical)
    canonical_id = parsed["canonical_id"]
    lat, lng, precision_m = derive_geo(parsed["country_iso2"], parsed["county"])

    if dry_run:
        log.info(
            "dry.parse",
            canonical_id=canonical_id,
            url=detail_url,
            country=parsed["country_iso2"],
            county=parsed["county"],
            landmark=parsed["landmark"],
            date=parsed["event_date"].isoformat() if parsed["event_date"] else parsed["date_uncertainty"],
            diameter_m=parsed["diameter_m"],
            lat=lat,
            lng=lng,
            precision_m=precision_m,
            n_images=len(parsed["image_relnames"]),
            first_image=parsed["image_relnames"][0] if parsed["image_relnames"] else None,
        )
        return "skip"

    # Idempotent ordering: source_record is the "done" marker; written last.
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
            db.table("formations").update(
                {"location_precision_m": precision_m}
            ).eq("id", str(fid)).execute()

    # Use the URL-derived initial_canonical as the source_record_id so
    # already_scraped() works regardless of whether the CCDB Ref differs.
    link_alias(
        fid,
        SLUG,
        initial_canonical,
        source_url=detail_url,
        is_primary=True,
    )

    base_dir = detail_url.rsplit("/", 1)[0]
    for relname in parsed["image_relnames"]:
        image_url = relname if "://" in relname else f"{base_dir}/{relname}"
        existing = (
            db.table("formation_images")
            .select("id")
            .eq("source_url", image_url)
            .limit(1)
            .execute()
            .data
        )
        if existing:
            continue
        db.table("formation_images").insert(
            {
                "formation_id": str(fid),
                "source_id": str(get_source_id(SLUG)),
                "source_url": image_url,
                "image_kind": "aerial",
                "license": "All rights reserved (copyright Lucy Pringle)",
                "license_notes": detail_url,
                "can_redistribute": False,
            }
        ).execute()

    upsert_source_record(
        source_slug=SLUG,
        source_record_id=initial_canonical,
        source_url=detail_url,
        raw_html=html,
        http_status=status,
    )

    return "inserted"


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lucy Pringle crop circle photo archive scraper.",
    )
    ap.add_argument("--start", type=int, default=1990, help="Start year (inclusive).")
    ap.add_argument("--end", type=int, default=2014, help="End year (inclusive).")
    ap.add_argument("--year", type=int, default=None, help="Single year shortcut.")
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

            links = extract_detail_links(listing_url, html)
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
