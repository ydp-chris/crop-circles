"""
geocode_formations.py — Resolve formation centroids to real GPS coords via
OpenStreetMap Nominatim.

Most rows in `crop_circles.formations` have a populated `nearest_landmark` +
`county` + `country` but only a county-level centroid (precision ~30 km). We
already trust the textual locality, so we just need to translate that text into
a real point. Nominatim is the free, no-key option; it's slow (1 req/sec) but
correct for the village/hamlet lookups we need.

Strategy:
- Pull formations with a landmark + country + non-precise location.
- Build query "<landmark>, <county>, <country_full_name>".
- Cache results on-disk so re-runs are cheap.
- Quality-gate: class must be place-like, importance >= 0.3, country must
  match, bbox diagonal < 10 km. Anything else is rejected and the centroid
  is left alone.
- On accept: cc_set_formation_location RPC + UPDATE location_precision_m=500.

Run:
    .venv/bin/python scripts/geocode_formations.py --dry-run --limit 20
    .venv/bin/python scripts/geocode_formations.py --limit 100
    .venv/bin/python scripts/geocode_formations.py --country GB --limit 500
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import httpx  # noqa: E402
import structlog  # noqa: E402

from db import db  # noqa: E402

log = structlog.get_logger()

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = (
    "YDP-CropCircles/0.1 "
    "(https://github.com/YDP-Chris; fordchristopheralan@gmail.com)"
)
THROTTLE_SECONDS = 1.1   # Nominatim policy: max 1 req/sec
NOMINATIM_TIMEOUT = 30.0

# Quality bar
ACCEPTED_CLASSES = {
    "place", "boundary", "historic", "tourism", "natural", "man_made"
}
MIN_IMPORTANCE = 0.3
MAX_BBOX_DIAGONAL_KM = 10.0

# Output precision (Nominatim village-level resolution is approximately this)
ACCEPT_PRECISION_M = 500

# Hard ceiling so a single invocation cannot run for hours.
HARD_SUCCESS_CEILING = 800

CACHE_PATH = ROOT / "data" / "nominatim_cache.json"

# ISO-2 -> full name. Covers everything we currently have in formations
# (sample showed GB, IT, DE, NL, CZ, BE, US, PL, FR, BR, CH, CA, RU, NO, SI,
# MX, AR, SK, AU, HR, ES, ID, SE, AT, UA, FI, BA, HU, LV, IN, DK, CN, NZ, etc.)
COUNTRY_NAMES: dict[str, str] = {
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "IT": "Italy",
    "DE": "Germany",
    "NL": "Netherlands",
    "CZ": "Czech Republic",
    "BE": "Belgium",
    "US": "United States",
    "PL": "Poland",
    "FR": "France",
    "BR": "Brazil",
    "CH": "Switzerland",
    "CA": "Canada",
    "RU": "Russia",
    "NO": "Norway",
    "SI": "Slovenia",
    "MX": "Mexico",
    "AR": "Argentina",
    "SK": "Slovakia",
    "AU": "Australia",
    "HR": "Croatia",
    "ES": "Spain",
    "ID": "Indonesia",
    "SE": "Sweden",
    "AT": "Austria",
    "UA": "Ukraine",
    "FI": "Finland",
    "BA": "Bosnia and Herzegovina",
    "HU": "Hungary",
    "LV": "Latvia",
    "IN": "India",
    "DK": "Denmark",
    "CN": "China",
    "NZ": "New Zealand",
    "ZA": "South Africa",
    "JP": "Japan",
    "IL": "Israel",
    "IE": "Ireland",
    "PT": "Portugal",
    "RO": "Romania",
    "BG": "Bulgaria",
    "GR": "Greece",
    "TR": "Turkey",
    "EE": "Estonia",
    "LT": "Lithuania",
    "RS": "Serbia",
    "MK": "North Macedonia",
    "ME": "Montenegro",
    "AL": "Albania",
    "BY": "Belarus",
    "MD": "Moldova",
    "GE": "Georgia",
    "AM": "Armenia",
    "AZ": "Azerbaijan",
    "KR": "South Korea",
    "TH": "Thailand",
    "MY": "Malaysia",
    "PH": "Philippines",
    "VN": "Vietnam",
    "EG": "Egypt",
    "MA": "Morocco",
    "ZW": "Zimbabwe",
    "CL": "Chile",
    "PE": "Peru",
    "CO": "Colombia",
    "UY": "Uruguay",
    "VE": "Venezuela",
    "EC": "Ecuador",
}


# ----------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------
def load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except json.JSONDecodeError:
            log.warning("cache.corrupt", path=str(CACHE_PATH))
    return {}


def save_cache(cache: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp.replace(CACHE_PATH)


# ----------------------------------------------------------------------------
# Candidate fetch
# ----------------------------------------------------------------------------
def fetch_candidates(
    country_filter: Optional[str], limit: Optional[int]
) -> list[dict[str, Any]]:
    """Return formations with landmark + country and non-exact location.

    Pages through PostgREST in 1000-row chunks (PostgREST default cap).
    """
    rows: list[dict[str, Any]] = []
    page_size = 1000
    offset = 0
    while True:
        q = (
            db.table("formations")
            .select(
                "id,canonical_id,nearest_landmark,county,country,"
                "lat,lng,location_precision_m"
            )
            .not_.is_("nearest_landmark", "null")
            .not_.is_("country", "null")
            .or_("location_precision_m.gt.1000,location_precision_m.is.null")
            .order("canonical_id")
            .range(offset, offset + page_size - 1)
        )
        if country_filter:
            q = q.eq("country", country_filter)
        page = q.execute().data
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        if limit is not None and len(rows) >= limit:
            break
    if limit is not None:
        rows = rows[:limit]
    return rows


# ----------------------------------------------------------------------------
# Query construction
# ----------------------------------------------------------------------------
import re  # noqa: E402

# "Nr Devizes" / "nr. Avebury" / "near Cherhill" — extract the village name.
# This is by far the most common landmark pattern in the dataset (Wiltshire
# crop circles are usually named "<field>, nr <village>").
_NR_PATTERN = re.compile(
    r"\b(?:n(?:r|ear)\.?\s+)([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3})",
    re.IGNORECASE,
)


def normalize_landmark(landmark: str) -> str:
    """Strip parenthetical noise and collapse whitespace."""
    s = re.sub(r"\([^)]*\)", "", landmark)
    return re.sub(r"\s+", " ", s).strip(" ,;-")


def extract_village_hint(landmark: str) -> Optional[str]:
    """Return the village name after 'nr'/'near' if present, else None."""
    m = _NR_PATTERN.search(landmark)
    if m:
        return m.group(1).strip(" ,;-")
    return None


def build_queries(
    landmark: str, county: Optional[str], country: str
) -> list[str]:
    """Return one or more query strings to try, most-specific first.

    1. Full landmark + county + country (primary).
    2. If landmark contains "nr X", just X + county + country (fallback).
    3. Just village portion before any comma + county + country (last resort).
    """
    landmark = normalize_landmark(landmark)
    county_clean = (county or "").strip() or None
    country_full = COUNTRY_NAMES.get(country, country)

    queries: list[str] = []

    def _push(parts: list[Optional[str]]) -> None:
        cleaned = [p for p in parts if p]
        # De-dup parts (avoid "X, X, GB").
        seen: set[str] = set()
        deduped: list[str] = []
        for p in cleaned:
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        q = ", ".join(deduped)
        if q and q not in queries:
            queries.append(q)

    _push([landmark, county_clean, country_full])

    village = extract_village_hint(landmark)
    if village:
        _push([village, county_clean, country_full])

    # First comma-segment as a coarse fallback (e.g. "Hackpen Hill, nr Broad
    # Hinton" -> "Hackpen Hill"); useful if the village hint missed.
    head = landmark.split(",", 1)[0].strip()
    if head and head.lower() != landmark.lower():
        _push([head, county_clean, country_full])

    return queries


# ----------------------------------------------------------------------------
# Nominatim
# ----------------------------------------------------------------------------
def nominatim_search(
    client: httpx.Client, query: str, country_iso2: str
) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "format": "json",
        "limit": 1,
        "addressdetails": 1,
    }
    # GB is the only ISO-2 alias Nominatim might quibble on; "uk" works too,
    # but it accepts "gb" lowercase per their docs.
    if country_iso2:
        params["countrycodes"] = country_iso2.lower()

    resp = client.get(NOMINATIM_URL, params=params)
    resp.raise_for_status()
    return resp.json()


def bbox_diagonal_km(bbox: list[str]) -> float:
    """Nominatim's boundingbox is [south, north, west, east] as strings."""
    try:
        s, n, w, e = (float(x) for x in bbox)
    except (TypeError, ValueError):
        return 0.0
    # Haversine across the diagonal corners.
    return haversine_km(s, w, n, e)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


# ----------------------------------------------------------------------------
# Quality gate
# ----------------------------------------------------------------------------
def evaluate(
    result: dict[str, Any], expected_country_iso2: str
) -> tuple[bool, str]:
    """Return (accepted, reason). Reason set on either path."""
    cls = result.get("class") or ""
    if cls not in ACCEPTED_CLASSES:
        return False, f"class:{cls or 'none'}"

    importance = float(result.get("importance") or 0.0)
    if importance < MIN_IMPORTANCE:
        return False, "importance"

    addr = result.get("address") or {}
    returned_cc = (addr.get("country_code") or "").upper()
    if returned_cc and returned_cc != expected_country_iso2.upper():
        return False, f"wrong_country:{returned_cc}"

    bbox = result.get("boundingbox") or []
    diag = bbox_diagonal_km(bbox) if bbox else 0.0
    if diag > MAX_BBOX_DIAGONAL_KM:
        return False, "bbox_too_large"

    return True, "ok"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to Supabase.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap total candidates considered.")
    ap.add_argument("--country", type=str, default=None,
                    help="Filter to a single ISO-2 country (e.g. GB).")
    args = ap.parse_args()

    cache = load_cache()
    cache_hits = 0
    cache_misses = 0

    start = time.monotonic()

    candidates = fetch_candidates(args.country, args.limit)
    log.info(
        "candidates.fetched",
        n=len(candidates),
        country=args.country,
        limit=args.limit,
    )

    accepted = 0
    rejected = 0
    rejection_reasons: Counter[str] = Counter()
    errors = 0
    surprising: list[dict[str, Any]] = []

    httpx_client = httpx.Client(
        timeout=NOMINATIM_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    last_request_at: float = 0.0

    try:
        for idx, row in enumerate(candidates, start=1):
            if accepted >= HARD_SUCCESS_CEILING:
                log.info("ceiling.hit", accepted=accepted)
                break

            landmark = (row.get("nearest_landmark") or "").strip()
            county = (row.get("county") or "").strip() or None
            country = (row.get("country") or "").strip().upper()
            if not landmark or not country:
                continue

            queries = build_queries(landmark, county, country)
            top: Optional[dict[str, Any]] = None
            chosen_query: Optional[str] = None
            last_reason = "no_results"
            rate_limited = False

            for query in queries:
                cache_key = f"{country}|{query}"
                cached = cache.get(cache_key)
                if cached is not None:
                    cache_hits += 1
                    results = cached.get("results", [])
                else:
                    cache_misses += 1
                    elapsed = time.monotonic() - last_request_at
                    if elapsed < THROTTLE_SECONDS:
                        time.sleep(THROTTLE_SECONDS - elapsed)
                    try:
                        results = nominatim_search(
                            httpx_client, query, country
                        )
                    except httpx.HTTPStatusError as exc:
                        errors += 1
                        log.warning(
                            "nominatim.http_error",
                            status=exc.response.status_code,
                            query=query,
                        )
                        if exc.response.status_code in (429, 403):
                            log.error(
                                "nominatim.rate_limited_or_blocked",
                                status=exc.response.status_code,
                            )
                            rate_limited = True
                            break
                        last_request_at = time.monotonic()
                        continue
                    except httpx.HTTPError as exc:
                        errors += 1
                        log.warning(
                            "nominatim.error", err=str(exc), query=query
                        )
                        last_request_at = time.monotonic()
                        continue
                    last_request_at = time.monotonic()
                    cache[cache_key] = {
                        "results": results,
                        "fetched_at": time.time(),
                    }
                    if cache_misses % 25 == 0:
                        save_cache(cache)

                if not results:
                    last_reason = "no_results"
                    continue

                candidate = results[0]
                ok, reason = evaluate(candidate, country)
                if ok:
                    top = candidate
                    chosen_query = query
                    break
                last_reason = reason

            if rate_limited:
                break

            if top is None:
                rejected += 1
                rejection_reasons[last_reason] += 1
                continue

            query = chosen_query or queries[0]

            try:
                lat = float(top["lat"])
                lng = float(top["lon"])
            except (KeyError, TypeError, ValueError):
                rejected += 1
                rejection_reasons["bad_coords"] += 1
                continue

            old_lat = row.get("lat")
            old_lng = row.get("lng")
            shift_km = (
                haversine_km(float(old_lat), float(old_lng), lat, lng)
                if old_lat is not None and old_lng is not None
                else None
            )

            entry = {
                "canonical_id": row["canonical_id"],
                "query": query,
                "lat": lat,
                "lng": lng,
                "shift_km": shift_km,
                "display_name": top.get("display_name"),
                "class": top.get("class"),
                "type": top.get("type"),
                "importance": top.get("importance"),
            }

            # Track "surprising" picks: anything that moved the centroid by
            # more than 30 km. Useful sanity-check signal for the report.
            if shift_km is not None and shift_km > 30:
                surprising.append(entry)

            if args.dry_run:
                log.info("accept.dry_run", **entry)
            else:
                try:
                    db.rpc(
                        "cc_set_formation_location",
                        {
                            "p_formation_id": row["id"],
                            "p_lat": lat,
                            "p_lng": lng,
                        },
                    ).execute()
                    db.table("formations").update(
                        {"location_precision_m": ACCEPT_PRECISION_M}
                    ).eq("id", row["id"]).execute()
                except Exception as exc:  # pragma: no cover  - log and continue
                    errors += 1
                    log.error(
                        "db.update_failed",
                        canonical_id=row["canonical_id"],
                        err=str(exc),
                    )
                    continue

            accepted += 1

            if accepted % 25 == 0:
                elapsed_s = time.monotonic() - start
                log.info(
                    "progress",
                    seen=idx,
                    accepted=accepted,
                    rejected=rejected,
                    cache_hit=cache_hits,
                    cache_miss=cache_misses,
                    elapsed_s=int(elapsed_s),
                )
    finally:
        httpx_client.close()
        save_cache(cache)

    elapsed = time.monotonic() - start

    # Sort surprising by shift desc, take top 5.
    surprising.sort(key=lambda x: x["shift_km"] or 0, reverse=True)
    top_surprising = surprising[:5]

    print()
    print("=" * 72)
    print("Geocoding summary")
    print("=" * 72)
    print(f"Candidates considered:  {len(candidates)}")
    print(f"Accepted (geocoded):    {accepted}")
    print(f"Rejected:               {rejected}")
    for reason, n in rejection_reasons.most_common():
        print(f"   - {reason:24s} {n}")
    if errors:
        print(f"Errors:                 {errors}")
    total_lookups = cache_hits + cache_misses
    if total_lookups:
        hit_rate = 100.0 * cache_hits / total_lookups
        print(
            f"Cache:                  {cache_hits} hits / "
            f"{cache_misses} misses ({hit_rate:.1f}% hit rate)"
        )
    print(f"Wall time:              {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print()
    if top_surprising:
        print("Top 5 most surprising geocoded results (largest centroid shift):")
        for s in top_surprising:
            print(
                f"  - {s['canonical_id']:18s} "
                f"shift={s['shift_km']:.0f}km  "
                f"({s['lat']:.4f}, {s['lng']:.4f})  "
                f"{s['query']}"
            )
    print()
    print(f"Cache file: {CACHE_PATH}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
