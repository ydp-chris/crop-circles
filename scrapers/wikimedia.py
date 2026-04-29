"""
scrapers/wikimedia.py — Crawl Wikimedia Commons for crop circle images.

Polite scraper:
  * Single User-Agent identifying the project
  * Throttled to ~2 req/sec
  * Resumes via already_scraped() — re-runs are no-ops for prior pages
  * Records full API response to source_records (jsonb) for re-extraction later

Day-1 strategy: one formation per Wikimedia file. Many real-world events have
multiple photos in Commons; a later dedup pass merges them via phash + location
proximity + event_date. Day 1 takes the noise; merge later.

CLI:
    .venv/bin/python -m scrapers.wikimedia --dry-run --limit 5
    .venv/bin/python -m scrapers.wikimedia --limit 50
    .venv/bin/python -m scrapers.wikimedia                 # full Category:Crop_circles
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
from tenacity import retry, stop_after_attempt, wait_exponential

# Allow running as `python scrapers/wikimedia.py` from project root.
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

log = structlog.get_logger("wikimedia")

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)
ROOT_CATEGORY = "Category:Crop_circles"
SLUG = "wikimedia"
THROTTLE_SECS = 0.5

CC_OK_RE = re.compile(
    r"(cc[-_ ]?by|cc[-_ ]?sa|cc[-_ ]?0|public[-_ ]?domain|publicdomain)",
    re.IGNORECASE,
)

# Titles that masquerade as formations but are UI/diagram assets in the
# Commons "Crop_circles" category. Discovered when 16 Nicolas Mollet map
# marker icons polluted the 2011-02-25 cluster.
NON_FORMATION_TITLE_RE = re.compile(
    r"map marker icon|\b(?:logo|symbol|infographic|chart|svg flag)\b",
    re.IGNORECASE,
)
LANDMARK_RE = re.compile(
    # "near Avebury, Wiltshire" — require a region suffix to filter out prose like
    # "near the Great Serpent" or "near a road"; the trailing "[A-Z]" anchors a
    # second capitalized word, which acts as a poor-man's region check.
    r"\bnear\s+(?P<place>[A-Z][A-Za-z\-\']+(?:\s+[A-Z][A-Za-z\-\']+){0,2})\s*,\s+[A-Z]"
)


# ============================================================================
# HTTP
# ============================================================================
def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        http2=True,
    )


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=1, max=30), reraise=True)
def api_get(client: httpx.Client, **params: Any) -> dict[str, Any]:
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    r = client.get(API, params=params)
    r.raise_for_status()
    return r.json()


# ============================================================================
# Pagination
# ============================================================================
def iter_category_files(
    client: httpx.Client,
    category: str = ROOT_CATEGORY,
    recurse: bool = True,
    seen_cats: Optional[set[str]] = None,
) -> Iterator[dict[str, Any]]:
    """Yield file pages within a category, optionally recursing into subcats."""
    if seen_cats is None:
        seen_cats = set()
    if category in seen_cats:
        return
    seen_cats.add(category)
    log.info("category.scan", category=category)

    cont: dict[str, Any] = {}
    while True:
        data = api_get(
            client,
            action="query",
            generator="categorymembers",
            gcmtitle=category,
            gcmtype="file",
            gcmlimit=50,
            prop="imageinfo|coordinates|info",
            iiprop="url|extmetadata|size|sha1|user|timestamp|mime",
            iiextmetadatafilter=(
                "DateTime|DateTimeOriginal|GPSLatitude|GPSLongitude|"
                "ImageDescription|License|LicenseShortName|LicenseUrl|"
                "Artist|Credit|UsageTerms|ObjectName"
            ),
            coprop="type|name|dim|country|region|globe",
            **cont,
        )
        time.sleep(THROTTLE_SECS)
        for page in (data.get("query") or {}).get("pages", []) or []:
            if page.get("missing"):
                continue
            yield page
        cont = data.get("continue") or {}
        if not cont:
            break

    if recurse:
        sub_cont: dict[str, Any] = {}
        while True:
            data = api_get(
                client,
                action="query",
                list="categorymembers",
                cmtitle=category,
                cmtype="subcat",
                cmlimit=50,
                **sub_cont,
            )
            time.sleep(THROTTLE_SECS)
            for sub in (data.get("query") or {}).get("categorymembers", []) or []:
                yield from iter_category_files(
                    client, sub["title"], recurse=True, seen_cats=seen_cats
                )
            sub_cont = data.get("continue") or {}
            if not sub_cont:
                break


# ============================================================================
# Parsing
# ============================================================================
def _strip_html(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip() or None


def _extmd_value(extmd: dict[str, Any], key: str) -> Optional[str]:
    block = extmd.get(key)
    if not block:
        return None
    return block.get("value")


def parse_date(extmd: dict[str, Any]) -> tuple[Optional[Date], Optional[str]]:
    """Return (parsed_date, uncertainty_text)."""
    raw = _extmd_value(extmd, "DateTimeOriginal") or _extmd_value(extmd, "DateTime")
    if not raw:
        return None, None
    s = _strip_html(raw) or ""
    # exif "2008:07:15 14:23:00"
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2})", s)
    if m:
        try:
            return Date(int(m[1]), int(m[2]), int(m[3])), None
        except ValueError:
            pass
    # ISO "2008-07-15"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return Date(int(m[1]), int(m[2]), int(m[3])), None
        except ValueError:
            pass
    # year only
    if re.match(r"^\d{4}\b", s):
        return None, s
    return None, s


def parse_coords(
    page: dict[str, Any], extmd: dict[str, Any]
) -> tuple[Optional[float], Optional[float]]:
    """Prefer file-page coordinates (curated), then fall back to EXIF GPS."""
    coords = page.get("coordinates") or []
    if coords:
        c = coords[0]
        try:
            return float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            pass
    lat = _extmd_value(extmd, "GPSLatitude")
    lon = _extmd_value(extmd, "GPSLongitude")
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            return None, None
    return None, None


def parse_country(page: dict[str, Any]) -> Optional[str]:
    coords = page.get("coordinates") or []
    if coords:
        c = coords[0]
        country = c.get("country")
        if country and len(country) == 2:
            return country.upper()
    return None


def parse_landmark(extmd: dict[str, Any]) -> Optional[str]:
    desc = _strip_html(_extmd_value(extmd, "ImageDescription"))
    if not desc:
        return None
    m = LANDMARK_RE.search(desc)
    return m.group("place") if m else None


def parse_license(
    extmd: dict[str, Any],
) -> tuple[Optional[str], bool, Optional[str]]:
    """Return (license_short, can_redistribute, license_url)."""
    short = _strip_html(_extmd_value(extmd, "LicenseShortName"))
    code = _strip_html(_extmd_value(extmd, "License"))
    url = _extmd_value(extmd, "LicenseUrl")
    test = " ".join(filter(None, [code, short]))
    can = bool(test and CC_OK_RE.search(test))
    return short, can, url


def parse_artist(extmd: dict[str, Any]) -> Optional[str]:
    return _strip_html(_extmd_value(extmd, "Artist")) or _strip_html(
        _extmd_value(extmd, "Credit")
    )


def classify_image_kind(title: str) -> Optional[str]:
    t = title.lower()
    if "aerial" in t:
        return "aerial"
    if "drone" in t:
        return "drone"
    if "satellite" in t:
        return "satellite"
    if "diagram" in t or "scheme" in t or "svg" in t:
        return "diagram"
    return None


# ============================================================================
# Per-file processing
# ============================================================================
def process_page(page: dict[str, Any], dry_run: bool = False) -> str:
    """Returns a status string: 'skip' | 'inserted' | 'failed'."""
    pageid = page.get("pageid")
    title = page.get("title")
    if pageid is None or not title:
        return "failed"

    if NON_FORMATION_TITLE_RE.search(title):
        # UI / diagram assets that share the Crop_circles category but
        # aren't real formations.
        return "skip"

    source_record_id = f"pageid:{pageid}"
    if not dry_run and already_scraped(SLUG, source_record_id):
        return "skip"

    iinfo_list = page.get("imageinfo") or []
    if not iinfo_list:
        return "failed"
    iinfo = iinfo_list[0]
    extmd = iinfo.get("extmetadata") or {}

    img_url = iinfo.get("url")
    width = iinfo.get("width")
    height = iinfo.get("height")
    sha1 = iinfo.get("sha1")
    photo_date, date_unc = parse_date(extmd)
    lat, lng = parse_coords(page, extmd)
    country = parse_country(page)
    landmark = parse_landmark(extmd)
    license_short, can_redistribute, license_url = parse_license(extmd)
    photographer = parse_artist(extmd)
    image_kind = classify_image_kind(title)

    canonical_id = f"WIKIMEDIA-{pageid}"
    file_page_url = f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}"

    if dry_run:
        log.info(
            "dry.parse",
            title=title,
            canonical_id=canonical_id,
            date=photo_date.isoformat() if photo_date else date_unc,
            lat=lat,
            lng=lng,
            country=country,
            landmark=landmark,
            license=license_short,
            can_redistribute=can_redistribute,
            photographer=photographer,
            image_kind=image_kind,
        )
        return "skip"

    # Order matters: do side effects first; source_records is the "done" marker
    # so re-runs after a mid-flight crash will retry cleanly.
    fid = get_formation_by_canonical_id(canonical_id)
    if not fid:
        fid = insert_formation(
            canonical_id=canonical_id,
            event_date=photo_date,
            country=country,
            nearest_landmark=landmark,
            lat=lat,
            lng=lng,
            notes=date_unc,
        )

    link_alias(
        fid,
        SLUG,
        source_record_id,
        source_url=file_page_url,
        is_primary=True,
    )

    if img_url:
        existing = (
            db.table("formation_images")
            .select("id")
            .eq("source_url", img_url)
            .limit(1)
            .execute()
            .data
        )
        if not existing:
            db.table("formation_images").insert(
                {
                    "formation_id": str(fid),
                    "source_id": str(get_source_id(SLUG)),
                    "source_url": img_url,
                    "photographer": photographer,
                    "photo_date": photo_date.isoformat() if photo_date else None,
                    "image_kind": image_kind,
                    "width": width,
                    "height": height,
                    "content_hash": sha1,
                    "license": license_short,
                    "license_notes": license_url,
                    "can_redistribute": can_redistribute,
                }
            ).execute()

    upsert_source_record(
        source_slug=SLUG,
        source_record_id=source_record_id,
        source_url=file_page_url,
        parsed_json=page,
        http_status=200,
    )

    return "inserted"


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Wikimedia Commons crop circles scraper.")
    ap.add_argument("--category", default=ROOT_CATEGORY, help="Root category to scan.")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N files.")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and log; do not write to the database.",
    )
    ap.add_argument(
        "--no-recurse",
        action="store_true",
        help="Skip subcategories (root category only).",
    )
    args = ap.parse_args()

    counts = {"inserted": 0, "skip": 0, "failed": 0}
    seen = 0

    with make_client() as client:
        for page in iter_category_files(
            client,
            category=args.category,
            recurse=not args.no_recurse,
        ):
            seen += 1
            try:
                status = process_page(page, dry_run=args.dry_run)
                counts[status] += 1
            except Exception as e:
                log.exception(
                    "page.failed", title=page.get("title"), error=str(e)
                )
                counts["failed"] += 1

            if args.limit and seen >= args.limit:
                break

    print(f"\nDone. Seen {seen} files.")
    for k, v in counts.items():
        print(f"  {k:10s} {v}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
