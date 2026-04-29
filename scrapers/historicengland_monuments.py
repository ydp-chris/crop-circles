"""
scrapers/historicengland_monuments.py — Import Historic England's Scheduled
Monuments dataset (~20K archaeologically significant sites in England) into
crop_circles.heritage_sites.

Densifies the heritage proximity signal beyond the OSM-only seed (5,495 sites
in the Wessex bbox). Scheduled Monuments are designations applied to the most
nationally important deliberately-created archaeological remains: barrows,
henges, hillforts, ruined abbeys, deserted medieval villages, etc.

Source:
    Discovered via data.gov.uk CKAN search for publisher=historic-england.
    The current ArcGIS Online view is:

      National_Heritage_List_for_England_NHLE_v02_VIEW/FeatureServer/6
      (host: services-eu1.arcgis.com/ZOdPfBS3aqqDYPUQ)

    Native CRS is EPSG:27700 (British National Grid). The ArcGIS REST API
    accepts outSR=4326 + returnCentroid=true so it reprojects polygons to
    WGS84 centroids server-side -- no pyproj dependency needed.

Schema notes:
    crop_circles.heritage_sites.osm_type has a check constraint that allows
    only ('node','way','relation'). We're stuffing a non-OSM source into this
    table, so we set osm_type='node' (most natural since centroids are
    points) and use the `source` column ('historicengland') for provenance.
    Yes, the column name is misnamed for non-OSM rows -- see the schema
    docstring in 0007_heritage_sites.sql for the original reasoning.

    osm_id = ListEntry (Historic England's unique integer ID).

License: UK Open Government Licence (OGL) v3.0.

CLI:
    .venv/bin/python -m scrapers.historicengland_monuments
    .venv/bin/python -m scrapers.historicengland_monuments --dry-run
    .venv/bin/python -m scrapers.historicengland_monuments --limit 5000
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
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import db  # noqa: E402

log = structlog.get_logger("he_monuments")

FEATURE_SERVICE = (
    "https://services-eu1.arcgis.com/ZOdPfBS3aqqDYPUQ/arcgis/rest/services/"
    "National_Heritage_List_for_England_NHLE_v02_VIEW/FeatureServer/6"
)
USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)
PAGE_SIZE = 2000  # FeatureServer maxRecordCount
MAX_TOTAL = 25000  # safety cap per project requirements
SOURCE_NAME = "historicengland"
LICENSE_NAME = "OGL v3.0"

# ---------------------------------------------------------------------------
# Site type classification — derived from the monument's name. The HE dataset
# does not include a category field, so we keyword-match against the name to
# bucket each row into one of the existing crop_circles.heritage_sites
# site_type values used by the OSM seed (see scrapers/heritage_sites.py).
# ---------------------------------------------------------------------------
SITE_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Most specific archaeological types first.
    (re.compile(r"\bstone\s+circle\b", re.I),                       "stone_circle"),
    (re.compile(r"\bstanding\s+stone[s]?\b", re.I),                 "standing_stone"),
    (re.compile(r"\b(?:henge|henges)\b", re.I),                     "henge"),
    (re.compile(r"\b(?:long\s+barrow|long\s+barrows)\b", re.I),     "long_barrow"),
    (re.compile(r"\b(?:bowl\s+barrow|bell\s+barrow|disc\s+barrow|saucer\s+barrow|round\s+barrow)s?\b", re.I), "round_barrow"),
    (re.compile(r"\b(?:barrow|barrows|tumulus|tumuli)\b", re.I),    "tumulus"),
    (re.compile(r"\b(?:cairn|cairns)\b", re.I),                     "cairn"),
    (re.compile(r"\b(?:hill\s*fort|hillforts?|promontory\s+fort)\b", re.I), "hillfort"),
    (re.compile(r"\b(?:roman\s+fort|fortlet|fortification|fortifications)\b", re.I), "fortification"),
    (re.compile(r"\b(?:earthworks?|enclosure|enclosures|cross-?dyke|dyke)\b", re.I), "earthworks"),
    (re.compile(r"\b(?:settlement|settlements|hut\s+circle|deserted\s+medieval\s+village|dmv)\b", re.I), "settlement"),
    (re.compile(r"\b(?:megalith|megalithic|cromlech|dolmen|chambered\s+tomb|chambered\s+cairn)\b", re.I), "megalith"),
    (re.compile(r"\b(?:castle|motte|bailey|keep)\b", re.I),         "castle"),
    (re.compile(r"\b(?:abbey|priory|monastery|nunnery|friary)\b", re.I), "ruins"),
    (re.compile(r"\b(?:moat|moated\s+site)\b", re.I),               "earthworks"),
    (re.compile(r"\b(?:cross|wayside\s+cross|preaching\s+cross)\b", re.I), "monument"),
]

# Period detection — best-effort, from name text.
PERIOD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:neolithic|new\s+stone\s+age)\b", re.I),  "Neolithic"),
    (re.compile(r"\b(?:mesolithic)\b", re.I),                   "Mesolithic"),
    (re.compile(r"\b(?:palaeolithic|paleolithic)\b", re.I),     "Palaeolithic"),
    (re.compile(r"\b(?:bronze\s+age)\b", re.I),                 "Bronze Age"),
    (re.compile(r"\b(?:iron\s+age)\b", re.I),                   "Iron Age"),
    (re.compile(r"\b(?:romano-?british|roman)\b", re.I),        "Roman"),
    (re.compile(r"\b(?:saxon|anglo-?saxon|early\s+medieval)\b", re.I), "Anglo-Saxon"),
    (re.compile(r"\b(?:medieval|mediaeval)\b", re.I),           "Medieval"),
    (re.compile(r"\b(?:post-?medieval|tudor|stuart|georgian)\b", re.I), "Post-Medieval"),
]


def classify_site_type(name: Optional[str]) -> str:
    if not name:
        return "archaeological_site"
    for pat, label in SITE_TYPE_PATTERNS:
        if pat.search(name):
            return label
    return "archaeological_site"


def classify_period(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    for pat, label in PERIOD_PATTERNS:
        if pat.search(name):
            return label
    return None


# ---------------------------------------------------------------------------
# ArcGIS REST client
# ---------------------------------------------------------------------------
def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=120.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=60), reraise=True)
def fetch_page(client: httpx.Client, offset: int, page_size: int) -> dict[str, Any]:
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "true",
        "returnCentroid": "true",
        "outSR": "4326",
        # Geometry data is large; skip rings to save bandwidth -- centroid
        # is what we keep. The API still requires returnGeometry=true to
        # populate centroid, so geometryType is a polygon ring envelope and
        # we just discard it.
        "resultOffset": str(offset),
        "resultRecordCount": str(page_size),
        "orderByFields": "OBJECTID ASC",
        "f": "json",
    }
    r = client.get(f"{FEATURE_SERVICE}/query", params=params)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"ArcGIS error: {j['error']}")
    return j


def fetch_total_count(client: httpx.Client) -> int:
    r = client.get(
        f"{FEATURE_SERVICE}/query",
        params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
    )
    r.raise_for_status()
    return int(r.json().get("count", 0))


def iter_features(client: httpx.Client, total_cap: int) -> Iterator[dict[str, Any]]:
    offset = 0
    seen = 0
    while seen < total_cap:
        page = fetch_page(client, offset, PAGE_SIZE)
        feats = page.get("features", []) or []
        if not feats:
            return
        for f in feats:
            if seen >= total_cap:
                return
            yield f
            seen += 1
        offset += len(feats)
        # ArcGIS sometimes flags exceededTransferLimit even when truly done;
        # the empty-feats check above handles termination correctly.


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------
def feature_to_row(feat: dict[str, Any]) -> Optional[dict[str, Any]]:
    attrs = feat.get("attributes") or {}
    list_entry = attrs.get("ListEntry")
    if list_entry is None:
        return None
    centroid = feat.get("centroid") or {}
    lng = centroid.get("x")
    lat = centroid.get("y")
    if lng is None or lat is None:
        return None
    name = (attrs.get("Name") or "").strip() or None
    site_type = classify_site_type(name)
    period = classify_period(name)
    # Compact provenance blob -- mirrors how the OSM scraper stashes raw tags.
    osm_tags = {
        "ListEntry": list_entry,
        "Name": name,
        "SchedDate": attrs.get("SchedDate"),
        "AmendDate": attrs.get("AmendDate"),
        "CaptureScale": attrs.get("CaptureScale"),
        "hyperlink": attrs.get("hyperlink"),
        "area_ha": attrs.get("area_ha"),
        "NGR": attrs.get("NGR"),
        "Easting": attrs.get("Easting"),
        "Northing": attrs.get("Northing"),
        "_source": "historicengland_NHLE_FeatureServer_layer6",
    }
    return {
        "osm_type": "node",  # forced by the heritage_sites check constraint
        "osm_id": int(list_entry),
        "name": name,
        "site_type": site_type,
        "historic_period": period,
        "lat": float(lat),
        "lng": float(lng),
        "osm_tags": osm_tags,
    }


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------
def upsert_batch(rows: list[dict[str, Any]]) -> int:
    inserted = 0
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
                    "location": f"SRID=4326;POINT({r['lng']} {r['lat']})",
                    "osm_tags": r["osm_tags"],
                    "source": SOURCE_NAME,
                    "license": LICENSE_NAME,
                }
            )
        res = (
            db.table("heritage_sites")
            .upsert(payload, on_conflict="osm_type,osm_id", ignore_duplicates=True)
            .execute()
        )
        inserted += len(res.data or [])
    return inserted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Historic England Scheduled Monuments importer.")
    ap.add_argument("--dry-run", action="store_true", help="Fetch + classify but do not insert.")
    ap.add_argument("--limit", type=int, default=MAX_TOTAL,
                    help=f"Cap total rows fetched (default {MAX_TOTAL}).")
    args = ap.parse_args()

    cap = min(args.limit, MAX_TOTAL)
    log.info("he.start", cap=cap, dry_run=args.dry_run)

    rows: list[dict[str, Any]] = []
    with make_client() as client:
        total = fetch_total_count(client)
        log.info("he.total_count", total=total)
        print(f"Server reports {total:,} Scheduled Monument records. Cap = {cap:,}.")

        t0 = time.monotonic()
        for feat in iter_features(client, cap):
            row = feature_to_row(feat)
            if row:
                rows.append(row)
            if len(rows) % 2000 == 0 and rows:
                print(f"  fetched {len(rows):,} ... ({time.monotonic()-t0:.1f}s)")

    print(f"\nFetched {len(rows):,} rows in {time.monotonic()-t0:.1f}s.")

    # Tally
    by_type: dict[str, int] = {}
    by_period: dict[str, int] = {}
    for r in rows:
        by_type[r["site_type"]] = by_type.get(r["site_type"], 0) + 1
        if r["historic_period"]:
            by_period[r["historic_period"]] = by_period.get(r["historic_period"], 0) + 1

    print("\nBy site_type:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"  {t:25s} {n:>6,}")
    print("\nBy historic_period (where derivable):")
    for p, n in sorted(by_period.items(), key=lambda x: -x[1]):
        print(f"  {p:25s} {n:>6,}")

    # Bbox
    if rows:
        lats = [r["lat"] for r in rows]
        lngs = [r["lng"] for r in rows]
        print(
            f"\nBbox: lat {min(lats):.4f}..{max(lats):.4f}  "
            f"lng {min(lngs):.4f}..{max(lngs):.4f}"
        )

    if args.dry_run:
        print("\nDry run -- nothing written.")
        return 0

    print("\nInserting into crop_circles.heritage_sites ...")
    n_inserted = upsert_batch(rows)
    print(f"Inserted (new rows): {n_inserted:,}")
    print("Now run: select crop_circles.cc_recompute_nearby_sites();")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
