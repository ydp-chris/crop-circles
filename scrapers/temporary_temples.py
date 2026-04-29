"""
scrapers/temporary_temples.py — Scrape temporarytemples.co.uk.

Steve and Karen Alexander's UK crop-circle archive. WordPress-backed: each
formation lives as a `project` custom post type, exposed via the public
WP REST API at:

    /wp-json/wp/v2/project           list (per_page=100, paginated)
    /wp-json/wp/v2/project/<id>      detail
    /wp-json/wp/v2/media/<id>        featured image (URL + EXIF subset)

Recon notes (2026-04-29):
  * 240 project posts; 231 are real formations, 9 are season-info pages
    that lack a parseable date (e.g. "Crop Circles 2024 | Pre-season Info").
  * Title format is consistent: "Landmark, County | Day Month Year".
  * Body text (Divi page builder) carries OS Grid Refs from 2022 onward,
    what-three-words from 2024 onward. Pre-2022 entries are county-only.
  * Images carry EXIF Artist/Copyright/DateTimeOriginal but NO GPS block.
  * robots.txt only disallows /wp-admin/. We are well-behaved.

Canonical IDs use TEMPLES-<slug>; we don't try to share IDs with CCC because
slugs are landmark-keyed (e.g. `cerne-abbas-2025`) and there's no clean
mapping to CCC's CC<YYYYMMDD>_<letter> scheme. Cross-archive merging is
the dedup pass's job.

Licensing: "STEVE ALEXANDER COPYRIGHT" in EXIF on every image — restrictive.
We record source URLs but set can_redistribute=False.

CLI:
    .venv/bin/python -m scrapers.temporary_temples --dry-run --limit 5
    .venv/bin/python -m scrapers.temporary_temples --limit 50
    .venv/bin/python -m scrapers.temporary_temples
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date as Date
from html import unescape
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
import structlog
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
from lib.centroids import lookup_country, lookup_uk_county  # noqa: E402

log = structlog.get_logger("temples")

BASE = "https://temporarytemples.co.uk"
API = f"{BASE}/wp-json/wp/v2"
USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)
SLUG = "temporary_temples"
THROTTLE_SECS = 1.5  # polite — they're a small private archive

MONTHS_FULL = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
MONTHS_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec"]
MONTHS_LOOKUP: dict[str, int] = {}
for i, m in enumerate(MONTHS_FULL, start=1):
    MONTHS_LOOKUP[m.lower()] = i
for m in MONTHS_ABBR:
    # Normalize "Sept" -> September (9), all other 3-letter abbreviations to
    # their full-month index.
    full_match = next(
        (i for i, fm in enumerate(MONTHS_FULL, start=1)
         if fm.lower().startswith(m.lower())),
        None,
    )
    if full_match:
        MONTHS_LOOKUP[m.lower()] = full_match

_MON_PATTERN = "|".join(MONTHS_FULL + MONTHS_ABBR)

TITLE_SPLIT_RE = re.compile(r"^(?P<loc>.+?)\s*\|\s*(?P<datepart>.+?)\s*$")
DATE_DMY_RE = re.compile(
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{_MON_PATTERN})"
    rf"(?:\s+(?P<year>\d{{4}}))?",
    re.IGNORECASE,
)
DATE_MDY_RE = re.compile(
    rf"(?P<mon>{_MON_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s+(?P<year>\d{{4}}))?",
    re.IGNORECASE,
)
SEASON_INFO_RE = re.compile(
    r"\b(?:pre[- ]season|season info|season overview|early season|"
    r"welcome to the)\b",
    re.IGNORECASE,
)
# OS grid: 2 letters + 4-5 digits + space + 4-5 digits (post-2022 entries)
OS_GRID_RE = re.compile(r"\b([A-Z]{2})\s*(\d{4,5})\s+(\d{4,5})\b")
# what-three-words: lowercase.lowercase.lowercase (post-2024-ish)
W3W_RE = re.compile(
    r"\b([a-z]{3,})\.([a-z]{3,})\.([a-z]{3,})\b"
)
DIAMETER_FT_RE = re.compile(
    r"(?:approximately|measures|measuring|measured|diameter)\s+"
    r"(?:approximately\s+)?(\d{2,4})\s*(?:ft|feet|')\b",
    re.IGNORECASE,
)
DIAMETER_M_RE = re.compile(
    r"(?:approximately|measures|measuring|measured|diameter)\s+"
    r"(?:approximately\s+)?(\d{2,4}(?:\.\d+)?)\s*m\b",
    re.IGNORECASE,
)
CROP_RE = re.compile(
    r"\b(?:in a |in a field of |in a crop of |field of |crop of )"
    r"(?P<crop>wheat|barley|oats|oilseed rape|rape|rye|corn|maize|"
    r"grass|linseed|hemp|cereals?)\b",
    re.IGNORECASE,
)

# Title/body county tokens that aren't in lib/centroids — Alexander uses
# British shorthand ("Wilts", "Hants", "Oxon" etc.) where the centroids
# table uses full names. Keep this scraper-local; the centroids table is
# canonical and shared with other scrapers.
COUNTY_ALIASES: dict[str, str] = {
    "wilts": "wiltshire",
    "wilts.": "wiltshire",
    "hants": "hampshire",
    "hants.": "hampshire",
    "oxon": "oxfordshire",
    "oxon.": "oxfordshire",
    "warks": "warwickshire",
    "warks.": "warwickshire",
    "glos": "gloucestershire",
    "glos.": "gloucestershire",
    "som": "somerset",
    "som.": "somerset",
    "bucks": "buckinghamshire",
    "bucks.": "buckinghamshire",
    "berks": "berkshire",
    "berks.": "berkshire",
    "beds": "bedfordshire",
    "beds.": "bedfordshire",
    "herts": "hertfordshire",
    "herts.": "hertfordshire",
    "northants": "northamptonshire",
    "northants.": "northamptonshire",
    "cambs": "cambridgeshire",
    "cambs.": "cambridgeshire",
    "lincs": "lincolnshire",
    "lincs.": "lincolnshire",
    "notts": "nottinghamshire",
    "notts.": "nottinghamshire",
    "leics": "leicestershire",
    "leics.": "leicestershire",
    "salop": "shropshire",
    "w. sussex": "west sussex",
    "w sussex": "west sussex",
    "e. sussex": "east sussex",
    "e sussex": "east sussex",
    "n. yorks": "north yorkshire",
    "s. yorks": "south yorkshire",
    "w. yorks": "west yorkshire",
    "e. yorks": "east yorkshire",
}


# ============================================================================
# HTTP
# ============================================================================
def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=30.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=30), reraise=True)
def api_get(client: httpx.Client, path: str, **params: Any) -> httpx.Response:
    r = client.get(f"{API}{path}", params=params)
    r.raise_for_status()
    return r


# ============================================================================
# Pagination
# ============================================================================
def iter_projects(client: httpx.Client) -> Iterator[dict[str, Any]]:
    """Yield bare project listings (no _embed, faster) with pagination."""
    page = 1
    while True:
        try:
            r = api_get(client, "/project", per_page=100, page=page,
                        orderby="date", order="desc")
        except httpx.HTTPStatusError as e:
            # WP returns 400 once page > total_pages
            if e.response.status_code == 400:
                break
            raise
        time.sleep(THROTTLE_SECS)
        data = r.json()
        if not data:
            break
        for p in data:
            yield p
        total_pages = int(r.headers.get("X-WP-TotalPages") or "1")
        if page >= total_pages:
            break
        page += 1


# ============================================================================
# Parsing
# ============================================================================
def _decode(s: str) -> str:
    """WP returns titles with HTML entities; unescape twice for &#038;-style."""
    return unescape(unescape(s or "")).strip()


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    # Divi shortcodes — strip [shortcode] and [shortcode foo="bar"]
    s = re.sub(r"\[/?[a-z_0-9]+[^\]]*\]", " ", s, flags=re.I)
    s = unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def is_season_info(title: str) -> bool:
    """Filter out non-formation editorial pages."""
    if SEASON_INFO_RE.search(title):
        return True
    # No pipe = not the standard formation title format
    if "|" not in title:
        return True
    return False


def parse_title(
    title: str, fallback_year: Optional[int] = None
) -> tuple[Optional[str], Optional[str], Optional[Date], Optional[str]]:
    """Return (landmark, county, event_date, raw_loc).

    fallback_year: if the title omits the year (rare — one historical entry
    "Tichborne, Hants | 24th June"), use this. Pass project['date'][:4]
    parsed as int.
    """
    m = TITLE_SPLIT_RE.match(title)
    if not m:
        return None, None, None, None
    loc = m.group("loc").strip()
    datepart = m.group("datepart").strip()

    # Date
    event_date: Optional[Date] = None
    dm = DATE_DMY_RE.search(datepart) or DATE_MDY_RE.search(datepart)
    if dm:
        try:
            day = int(dm.group("day"))
            mon = MONTHS_LOOKUP.get(dm.group("mon").lower())
            year_s = dm.group("year")
            # Title without year: borrow from raw datepart 4-digit year, then
            # fall back to fallback_year (typically the WP post date year).
            if not year_s:
                ym = re.search(r"\b(20\d{2})\b", datepart)
                year_s = ym.group(1) if ym else None
            if not year_s and fallback_year:
                year_s = str(fallback_year)
            if mon and year_s:
                event_date = Date(int(year_s), mon, day)
        except (ValueError, AttributeError):
            event_date = None

    # Location: "Landmark, ..., County" — split on commas. The last token is
    # almost always the county (full or abbr). Everything before is landmark
    # context (which may itself be comma-delimited; keep the first token as
    # the canonical landmark).
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    landmark: Optional[str] = parts[0] if parts else None
    county: Optional[str] = None
    if len(parts) >= 2:
        # Tail token is most likely the county; strip trailing parenthetical
        # noise like "Wilts (2)", "Hants (1)".
        tail = parts[-1]
        tail_clean = re.sub(r"\s*\([^)]*\)\s*$", "", tail).strip()
        county = tail_clean.lower() or None
    elif landmark:
        # Fallback for missing-comma typos like "Popham Hants" — if the last
        # word is a known county shortform, peel it off.
        words = landmark.split()
        if len(words) >= 2:
            last_word = words[-1].lower()
            if (last_word in COUNTY_ALIASES
                    or last_word in {
                        "wiltshire", "hampshire", "somerset", "dorset",
                        "oxfordshire", "warwickshire", "gloucestershire",
                        "berkshire", "buckinghamshire", "surrey", "devon",
                    }):
                county = last_word
                landmark = " ".join(words[:-1])

    return landmark, county, event_date, loc


def normalize_county(county: Optional[str]) -> Optional[str]:
    if not county:
        return None
    key = county.strip().lower()
    return COUNTY_ALIASES.get(key, key)


def derive_geo(
    county: Optional[str],
) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """UK county centroid -> country fallback. Mirrors cropcirclecenter logic."""
    if county:
        norm = normalize_county(county)
        hit = lookup_uk_county(norm)
        if hit:
            return hit
    # Default GB fallback for this archive (Alexander's work is UK-only)
    hit = lookup_country("GB")
    if hit:
        return hit
    return None, None, None


def parse_body(content_html: str) -> dict[str, Any]:
    """Pull OS grid, w3w, diameter, crop, first-reported date from post body."""
    text = _strip_html(content_html)
    out: dict[str, Any] = {
        "os_grid": None, "w3w": None,
        "diameter_m": None, "crop_type": None,
        "body_excerpt": text[:600] if text else None,
    }

    g = OS_GRID_RE.search(text)
    if g:
        out["os_grid"] = f"{g.group(1)} {g.group(2)} {g.group(3)}"

    # Word-three-words: be cautious — a generic regex over arbitrary prose
    # produces lots of false positives ("e.g." style abbreviations etc).
    # Anchor on the literal "what-three-words" / "what3words" prefix when
    # present, else fall back to a general scan over the first 1500 chars.
    head = text[:2000].lower()
    if "what" in head and "three" in head and "word" in head or "what3words" in head:
        # find first w3w pattern in the head
        m = W3W_RE.search(text[:2000])
        if m:
            cand = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
            # Cheap sanity: must not start with common english stopwords
            if cand not in ("e.g.eg", "etc.etc.etc"):
                out["w3w"] = cand

    dm = DIAMETER_M_RE.search(text)
    if dm:
        try:
            out["diameter_m"] = float(dm.group(1))
        except ValueError:
            pass
    if out["diameter_m"] is None:
        df = DIAMETER_FT_RE.search(text)
        if df:
            try:
                out["diameter_m"] = round(int(df.group(1)) * 0.3048, 1)
            except ValueError:
                pass

    cm = CROP_RE.search(text)
    if cm:
        out["crop_type"] = cm.group("crop").lower()

    return out


def fetch_featured_media(
    client: httpx.Client, media_id: int
) -> Optional[dict[str, Any]]:
    if not media_id:
        return None
    try:
        r = api_get(client, f"/media/{media_id}")
    except Exception as e:
        log.warning("media.fetch_failed", id=media_id, error=str(e))
        return None
    time.sleep(THROTTLE_SECS)
    m = r.json()
    md = m.get("media_details") or {}
    im = md.get("image_meta") or {}
    # Empty string is the WP default; treat as missing.
    credit = (im.get("credit") or "").strip() or None
    copyr = (im.get("copyright") or "").strip() or None
    ts_raw = (im.get("created_timestamp") or "").strip() or None
    # WP returns "0" when EXIF lacks the timestamp; reject epoch-zero.
    exif_ts = None
    if ts_raw and ts_raw != "0":
        try:
            exif_ts = int(ts_raw)
            if exif_ts <= 0:
                exif_ts = None
        except ValueError:
            exif_ts = None
    return {
        "url": m.get("source_url"),
        "width": md.get("width"),
        "height": md.get("height"),
        # Default to Steve Alexander — every image on the archive is his work
        # (the EXIF on his post-2018 uploads consistently says
        # "STEVE-ALEXANDER COPYRIGHT" but older media records omit it).
        "photographer": credit or copyr or "Steve Alexander",
        "exif_timestamp": exif_ts,
    }


# ============================================================================
# Per-formation processing
# ============================================================================
def process_project(
    client: httpx.Client,
    project: dict[str, Any],
    dry_run: bool = False,
) -> str:
    pid = project.get("id")
    slug_raw = project.get("slug")
    title_raw = project.get("title")
    if pid is None or not slug_raw or not title_raw:
        return "failed"
    title = _decode(
        title_raw if isinstance(title_raw, str) else title_raw.get("rendered", "")
    )
    if is_season_info(title):
        return "skip"

    canonical_id = f"TEMPLES-{slug_raw}"
    if not dry_run and already_scraped(SLUG, canonical_id):
        return "skip"

    # Fallback year from the WP post.date (e.g. "2017-07-15T...") for the
    # rare entry that omits a year ("Tichborne, Hants | 24th June").
    fallback_year: Optional[int] = None
    pdate = project.get("date") or ""
    if isinstance(pdate, str) and len(pdate) >= 4 and pdate[:4].isdigit():
        fallback_year = int(pdate[:4])

    landmark, county, event_date, raw_loc = parse_title(title, fallback_year)
    if event_date is None:
        # Skip undated entries — they're either editorial or so malformed
        # we can't reliably place them on a timeline.
        log.info("skip.no_date", id=pid, title=title)
        return "skip"

    content_html = (project.get("content") or {}).get("rendered", "")
    body = parse_body(content_html)

    media_info: Optional[dict[str, Any]] = None
    fmid = project.get("featured_media")
    if fmid:
        media_info = fetch_featured_media(client, fmid)

    lat, lng, precision_m = derive_geo(county)

    detail_url = project.get("link") or f"{BASE}/project/{slug_raw}"

    # Diameter sanity: realistic crop circles are 10-200m. Kill obvious noise
    # from prose like "Britain's largest 180ft figure" picked up out of context.
    diameter_m = body.get("diameter_m")
    if diameter_m is not None and not (5 <= diameter_m <= 250):
        diameter_m = None

    notes_parts: list[str] = []
    if body.get("os_grid"):
        notes_parts.append(f"OS Grid: {body['os_grid']}")
    if body.get("w3w"):
        notes_parts.append(f"w3w: {body['w3w']}")
    if raw_loc and raw_loc != landmark:
        notes_parts.append(f"Title location: {raw_loc}")
    notes = " | ".join(notes_parts) or None

    if dry_run:
        log.info(
            "dry.parse",
            id=pid,
            slug=slug_raw,
            canonical_id=canonical_id,
            title=title,
            landmark=landmark,
            county=county,
            event_date=event_date.isoformat() if event_date else None,
            lat=lat,
            lng=lng,
            precision_m=precision_m,
            os_grid=body.get("os_grid"),
            w3w=body.get("w3w"),
            crop=body.get("crop_type"),
            diameter_m=diameter_m,
            image_url=(media_info or {}).get("url"),
            photographer=(media_info or {}).get("photographer"),
        )
        return "skip"

    # Idempotent side effects, source_records last as the "done" marker
    fid = get_formation_by_canonical_id(canonical_id)
    if not fid:
        fid = insert_formation(
            canonical_id=canonical_id,
            event_date=event_date,
            country="GB",
            county=normalize_county(county),
            nearest_landmark=landmark,
            lat=lat,
            lng=lng,
            crop_type=body.get("crop_type"),
            diameter_m=diameter_m,
            notes=notes,
        )
        if precision_m is not None:
            db.table("formations").update(
                {"location_precision_m": precision_m}
            ).eq("id", str(fid)).execute()

    link_alias(fid, SLUG, canonical_id, source_url=detail_url, is_primary=True)

    if media_info and media_info.get("url"):
        existing = (
            db.table("formation_images")
            .select("id")
            .eq("source_url", media_info["url"])
            .limit(1)
            .execute()
            .data
        )
        if not existing:
            row = {
                "formation_id": str(fid),
                "source_id": str(get_source_id(SLUG)),
                "source_url": media_info["url"],
                "image_kind": "aerial",  # Alexander shoots aerial almost exclusively
                "photographer": media_info.get("photographer"),
                "width": media_info.get("width"),
                "height": media_info.get("height"),
                "license": "All rights reserved (temporarytemples.co.uk)",
                "license_notes": detail_url,
                "can_redistribute": False,
            }
            # photo_date from EXIF if available
            ts = media_info.get("exif_timestamp")
            if ts:
                try:
                    row["photo_date"] = Date.fromtimestamp(int(ts)).isoformat()
                except (ValueError, OSError):
                    pass
            db.table("formation_images").insert(row).execute()

    # parsed_json carries the WP project payload as the immutable record
    upsert_source_record(
        source_slug=SLUG,
        source_record_id=canonical_id,
        source_url=detail_url,
        parsed_json={
            "wp_id": pid,
            "slug": slug_raw,
            "title": title,
            "date": project.get("date"),
            "modified": project.get("modified"),
            "project_category": project.get("project_category", []),
            "featured_media_id": fmid,
            "media": media_info,
            "parsed": {
                "landmark": landmark,
                "county": county,
                "event_date": event_date.isoformat() if event_date else None,
                **{k: body.get(k) for k in
                   ("os_grid", "w3w", "diameter_m", "crop_type")},
            },
        },
        http_status=200,
    )
    return "inserted"


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="temporarytemples.co.uk scraper.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N projects.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and log; do not write to DB.")
    args = ap.parse_args()

    counts = {"inserted": 0, "skip": 0, "failed": 0}
    seen = 0

    with make_client() as client:
        for project in iter_projects(client):
            seen += 1
            try:
                res = process_project(client, project, dry_run=args.dry_run)
                counts[res] += 1
            except Exception as e:
                log.exception(
                    "project.failed", id=project.get("id"),
                    slug=project.get("slug"), error=str(e),
                )
                counts["failed"] += 1
            if args.limit and seen >= args.limit:
                break

    print(f"\nDone. Seen {seen} projects.")
    for k, v in counts.items():
        print(f"  {k:10s} {v}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
