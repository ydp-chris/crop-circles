"""
scrapers/heritage_sites.py — Pull historic / religious sites from OSM Overpass.

Quickstart scope: Wessex bbox (covers Wiltshire, Hampshire, Oxfordshire,
Berkshire, Dorset, Somerset, Surrey, Sussex, Kent — UK crop-circle hotspot
counties). Other regions can be added later by extending BBOXES.

Licensing: OSM data is ODbL. Public site must attribute "© OpenStreetMap
contributors" wherever this data is displayed. We persist the source +
license per row so attribution stays correct downstream.

CLI:
    .venv/bin/python -m scrapers.heritage_sites --bbox wessex
    .venv/bin/python -m scrapers.heritage_sites --bbox wessex --dry-run
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db  # noqa: E402

log = structlog.get_logger("heritage")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)

# (south, west, north, east)
BBOXES: dict[str, tuple[float, float, float, float]] = {
    # Southern England — Wiltshire, Hampshire, Oxfordshire, Berkshire,
    # Dorset, Somerset, Surrey, Sussex, parts of Kent.
    "wessex": (50.50, -3.50, 52.00, 0.50),
    # Just Wiltshire (tighter, faster — for testing)
    "wiltshire": (50.95, -2.40, 51.70, -1.45),
}

# OSM historic= subtypes worth keeping. Filters out things like memorials to
# WWII pilots and recent statues.
RELEVANT_HISTORIC = {
    "archaeological_site",
    "megalith",
    "tumulus",
    "stone",
    "monument",
    "ruins",
    "castle",
    "fort",
    "manor",
    "city_gates",
}

# Specific archaeological-site types worth tagging more precisely.
ARCH_SITE_TYPES = {
    "henge",
    "tumulus",
    "barrow",
    "long_barrow",
    "round_barrow",
    "stone_circle",
    "standing_stone",
    "hillfort",
    "hill_fort",
    "settlement",
    "fortification",
    "cairn",
    "earthworks",
}


# ============================================================================
# Overpass
# ============================================================================
def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=120.0,
        headers={"User-Agent": USER_AGENT},
    )


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=60), reraise=True)
def overpass_query(client: httpx.Client, ql: str) -> dict[str, Any]:
    r = client.post(OVERPASS_URL, content=ql.encode("utf-8"))
    r.raise_for_status()
    return r.json()


def build_query(bbox: tuple[float, float, float, float]) -> str:
    s, w, n, e = bbox
    box = f"({s},{w},{n},{e})"
    # Pull historic features + place_of_worship (for old churches).
    return f"""
    [out:json][timeout:90];
    (
      node["historic"]{box};
      way["historic"]{box};
      relation["historic"]{box};
      node["amenity"="place_of_worship"]{box};
      way["amenity"="place_of_worship"]{box};
    );
    out center tags;
    """.strip()


# ============================================================================
# Normalization
# ============================================================================
def normalize_site_type(tags: dict[str, str]) -> Optional[str]:
    """High-signal filter. Drop unnamed catch-all + modern churches.

    Crop-circle-relevant sites are megalithic, archaeological, fortified, or
    ruined. Modern places of worship dilute the proximity signal (every
    English village has one). We keep named place_of_worship rows that look
    historic (have wikidata link or a "religion" tag indicating non-modern
    use) — most of those are pre-Norman churches.
    """
    h = tags.get("historic")
    if h:
        if h == "archaeological_site":
            site = (tags.get("site_type") or "").lower()
            if site in ARCH_SITE_TYPES:
                return site
            return "archaeological_site"
        if h in RELEVANT_HISTORIC:
            return h
        # historic=yes / historic=memorial / historic=building etc. — drop
        return None
    if tags.get("amenity") == "place_of_worship":
        # Quickstart: drop places of worship entirely. Most crop-circle
        # clustering claims are about megalithic / archaeological sites,
        # not parish churches. Adding pre-Norman churches as a separate
        # dataset (Historic England Listed Buildings) is a Phase 2 task.
        return None
    return None


def parse_period(tags: dict[str, str]) -> Optional[str]:
    """Best-effort archaeological period from OSM tags."""
    for k in ("period", "start_date", "historic:civilization"):
        v = tags.get(k)
        if v:
            return str(v).strip()
    return None


def name_from_tags(tags: dict[str, str]) -> Optional[str]:
    return (
        tags.get("name")
        or tags.get("name:en")
        or tags.get("official_name")
        or tags.get("alt_name")
    )


# ============================================================================
# Per-element processing
# ============================================================================
def latlon_from_element(el: dict[str, Any]) -> Optional[tuple[float, float]]:
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    c = el.get("center") or {}
    if "lat" in c and "lon" in c:
        return float(c["lat"]), float(c["lon"])
    return None


def iter_seed_rows(elements: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for el in elements:
        tags = el.get("tags") or {}
        site_type = normalize_site_type(tags)
        if not site_type:
            continue
        ll = latlon_from_element(el)
        if not ll:
            continue
        yield {
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
            "name": name_from_tags(tags),
            "site_type": site_type,
            "historic_period": parse_period(tags),
            "lat": ll[0],
            "lng": ll[1],
            "tags": tags,
        }


# ============================================================================
# Insert helpers
# ============================================================================
def upsert_site_batch(rows: list[dict[str, Any]]) -> int:
    """Insert via PostgREST; PostGIS WKT for the geography column."""
    inserted = 0
    # Batch in groups of 500 to keep payloads reasonable.
    for i in range(0, len(rows), 500):
        batch = rows[i : i + 500]
        payload = []
        for r in batch:
            payload.append(
                {
                    "osm_type": r["osm_type"],
                    "osm_id": r["osm_id"],
                    "name": r["name"],
                    "site_type": r["site_type"],
                    "historic_period": r["historic_period"],
                    # PostGIS accepts WKT-with-SRID via implicit cast on insert
                    "location": f"SRID=4326;POINT({r['lng']} {r['lat']})",
                    "osm_tags": r["tags"],
                }
            )
        res = (
            db.table("heritage_sites")
            .upsert(payload, on_conflict="osm_type,osm_id", ignore_duplicates=True)
            .execute()
        )
        inserted += len(res.data or [])
    return inserted


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="OSM heritage seed.")
    ap.add_argument("--bbox", default="wessex", choices=list(BBOXES.keys()))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bbox = BBOXES[args.bbox]
    log.info("overpass.start", bbox=args.bbox, coords=bbox)

    with make_client() as client:
        ql = build_query(bbox)
        data = overpass_query(client, ql)

    elements = data.get("elements") or []
    log.info("overpass.received", elements=len(elements))

    rows = list(iter_seed_rows(elements))
    log.info("overpass.kept", rows=len(rows))

    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["site_type"]] = by_type.get(r["site_type"], 0) + 1
    print("\nBy site type:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t:25s} {n}")

    if args.dry_run:
        print("\nDry run — nothing written.")
        return 0

    n_inserted = upsert_site_batch(rows)
    print(f"\nInserted: {n_inserted}")
    print("Now run: select crop_circles.cc_recompute_nearby_sites();")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
