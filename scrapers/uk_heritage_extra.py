"""
scrapers/uk_heritage_extra.py — Import Welsh (Cadw) and Scottish (HES)
Scheduled Monument registers into crop_circles.heritage_sites.

Completes UK coverage to match the Historic England import (see
historicengland_monuments.py for the English equivalent and the schema
notes that apply equally here).

Sources discovered (working endpoints as of 2026-04-29):

  Cadw — DataMapWales WFS (GeoServer):
    https://datamap.gov.wales/geoserver/wfs
      ?service=WFS&version=2.0.0&request=GetFeature
      &typeNames=inspire-wg:Cadw_SAM
      &outputFormat=application/json&srsName=EPSG:4326
    4,236 records. Properties include RecordNumber (int), SAMNumber
    (the human Cadw code, e.g. 'CN395', 'ME249'), Name, BroadClass,
    Period, SiteType, UnitaryAuthority, Report (link), easting/northing
    (BNG, occasionally null). Geometry is MultiPolygon already
    reprojected to EPSG:4326 — we average ring vertices to get a
    centroid (good enough for monument-scale polygons; bbox center as
    fallback). License: OGL v3.0.

  HES — INSPIRE MapServer hosted at inspire.hes.scot:
    https://inspire.hes.scot/arcgis/rest/services/INSPIRE/
      Scottish_Cultural_ProtectedSites/MapServer/3
    8,073 records. Layer 3 = "Scheduled Monuments". Fields include
    inspireID (e.g. 'SM5736' — strip 'SM' for integer), siteName,
    designatio (= 'Scheduled Monument'), legalFou_1 (legal date code).
    The MapServer rejects resultRecordCount-style pagination, so we
    page by FID range (0..max in 1000-row chunks). Geometry is
    polygons in EPSG:4326 — averaged client-side for centroids.
    License: OGL v3.0.

ID namespacing (important):
    The heritage_sites uniqueness constraint is (osm_type, osm_id) —
    source is NOT part of the key. Cadw RecordNumber lives in 1..~4250,
    HES SM number lives in 1..~90000, and Historic England ListEntry
    lives in 1_000_000..1_500_000. Cadw and HES collide heavily, so we
    offset HES integer IDs by HES_ID_OFFSET (10_000_000) when writing
    to the table. The original SM number is preserved verbatim in
    osm_tags['inspireID']. To recover the original SM int from a row:
    osm_id - 10_000_000.

Schema notes mirror historicengland_monuments.py:
  - osm_type='node' (only allowed values: node|way|relation)
  - osm_id=integer parsed from each source's record number
  - source='cadw' | 'hes'
  - location written as 'SRID=4326;POINT(lng lat)'

Idempotent on (osm_type, osm_id) via upsert with ignore_duplicates.

CLI:
    .venv/bin/python -m scrapers.uk_heritage_extra --source cadw
    .venv/bin/python -m scrapers.uk_heritage_extra --source hes
    .venv/bin/python -m scrapers.uk_heritage_extra --source both
    .venv/bin/python -m scrapers.uk_heritage_extra --source cadw --dry-run
    .venv/bin/python -m scrapers.uk_heritage_extra --source hes --limit 1000
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

log = structlog.get_logger("uk_heritage_extra")

USER_AGENT = (
    "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; "
    "fordchristopheralan@gmail.com)"
)
MAX_TOTAL = 25000
LICENSE_NAME = "OGL v3.0"

CADW_WFS = "https://datamap.gov.wales/geoserver/wfs"
CADW_TYPENAME = "inspire-wg:Cadw_SAM"

HES_LAYER = (
    "https://inspire.hes.scot/arcgis/rest/services/INSPIRE/"
    "Scottish_Cultural_ProtectedSites/MapServer/3"
)
HES_PAGE_SIZE = 1000  # MapServer maxRecordCount
HES_ID_OFFSET = 10_000_000  # see module docstring — namespacing vs Cadw collisions


# ---------------------------------------------------------------------------
# Site type / period classification — re-uses the Historic England regex
# library since the data is the same shape (Scheduled Monument names).
# Cadw also exposes a SiteType field; we prefer name-based classification for
# consistency with the OSM/HE rows but fall back to SiteType if the name
# yields nothing useful.
# ---------------------------------------------------------------------------
SITE_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bstone\s+circle\b", re.I),                       "stone_circle"),
    (re.compile(r"\bstanding\s+stone[s]?\b", re.I),                 "standing_stone"),
    (re.compile(r"\b(?:henge|henges)\b", re.I),                     "henge"),
    (re.compile(r"\b(?:long\s+barrow|long\s+barrows)\b", re.I),     "long_barrow"),
    (re.compile(
        r"\b(?:bowl\s+barrow|bell\s+barrow|disc\s+barrow|saucer\s+barrow|round\s+barrow)s?\b",
        re.I), "round_barrow"),
    (re.compile(r"\b(?:barrow|barrows|tumulus|tumuli)\b", re.I),    "tumulus"),
    (re.compile(r"\b(?:cairn|cairns)\b", re.I),                     "cairn"),
    (re.compile(r"\b(?:broch|brochs|dun|duns)\b", re.I),            "broch"),
    (re.compile(r"\b(?:crannog|crannogs)\b", re.I),                 "crannog"),
    (re.compile(r"\b(?:souterrain|souterrains)\b", re.I),           "souterrain"),
    (re.compile(r"\b(?:hill\s*fort|hillforts?|promontory\s+fort)\b", re.I), "hillfort"),
    (re.compile(r"\b(?:roman\s+fort|fortlet|fortification|fortifications)\b", re.I), "fortification"),
    (re.compile(
        r"\b(?:earthworks?|enclosure|enclosures|cross-?dyke|dyke)\b",
        re.I), "earthworks"),
    (re.compile(
        r"\b(?:settlement|settlements|hut\s+circle|deserted\s+medieval\s+village|dmv)\b",
        re.I), "settlement"),
    (re.compile(
        r"\b(?:megalith|megalithic|cromlech|dolmen|chambered\s+tomb|chambered\s+cairn)\b",
        re.I), "megalith"),
    (re.compile(r"\b(?:castle|motte|bailey|keep)\b", re.I),         "castle"),
    (re.compile(r"\b(?:abbey|priory|monastery|nunnery|friary|chapel|church)\b", re.I), "ruins"),
    (re.compile(r"\b(?:moat|moated\s+site)\b", re.I),               "earthworks"),
    (re.compile(r"\b(?:cross|wayside\s+cross|preaching\s+cross)\b", re.I), "monument"),
    (re.compile(r"\b(?:pictish|symbol\s+stone)\b", re.I),           "standing_stone"),
]

PERIOD_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:neolithic|new\s+stone\s+age)\b", re.I),  "Neolithic"),
    (re.compile(r"\b(?:mesolithic)\b", re.I),                   "Mesolithic"),
    (re.compile(r"\b(?:palaeolithic|paleolithic)\b", re.I),     "Palaeolithic"),
    (re.compile(r"\b(?:bronze\s+age)\b", re.I),                 "Bronze Age"),
    (re.compile(r"\b(?:iron\s+age)\b", re.I),                   "Iron Age"),
    (re.compile(r"\b(?:romano-?british|roman)\b", re.I),        "Roman"),
    (re.compile(r"\b(?:saxon|anglo-?saxon|early\s+medieval|pictish)\b", re.I),
                                                                "Early Medieval"),
    (re.compile(r"\b(?:medieval|mediaeval)\b", re.I),           "Medieval"),
    (re.compile(r"\b(?:post-?medieval|tudor|stuart|georgian|jacobite)\b", re.I),
                                                                "Post-Medieval"),
    (re.compile(r"\b(?:prehistoric)\b", re.I),                  "Prehistoric"),
]


def classify_site_type(name: Optional[str], fallback_hint: Optional[str] = None) -> str:
    text = " ".join(filter(None, [name, fallback_hint]))
    if not text:
        return "archaeological_site"
    for pat, label in SITE_TYPE_PATTERNS:
        if pat.search(text):
            return label
    return "archaeological_site"


def classify_period(name: Optional[str], fallback_hint: Optional[str] = None) -> Optional[str]:
    text = " ".join(filter(None, [name, fallback_hint]))
    if not text:
        return None
    for pat, label in PERIOD_PATTERNS:
        if pat.search(text):
            return label
    return None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=180.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


# ---------------------------------------------------------------------------
# Centroid helpers — both sources return polygons in EPSG:4326, so we just
# average vertices. Good enough for tagging proximity to crop circles; we are
# not doing anything that requires the true geometric centroid.
# ---------------------------------------------------------------------------
def _avg_centroid_from_rings(coords: list) -> Optional[tuple[float, float]]:
    """Walks any depth of ring/coord nesting, accumulates lng/lat sum/count."""
    sx = sy = 0.0
    n = 0

    def walk(c):
        nonlocal sx, sy, n
        if not c:
            return
        if (
            isinstance(c, list)
            and len(c) >= 2
            and all(isinstance(x, (int, float)) for x in c[:2])
        ):
            sx += c[0]
            sy += c[1]
            n += 1
            return
        if isinstance(c, list):
            for item in c:
                walk(item)

    walk(coords)
    if n == 0:
        return None
    return (sx / n, sy / n)


def geojson_centroid(geometry: Optional[dict]) -> Optional[tuple[float, float]]:
    if not geometry:
        return None
    g = geometry
    t = g.get("type")
    if t in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"):
        return _avg_centroid_from_rings(g.get("coordinates"))
    if t == "Point":
        c = g.get("coordinates") or []
        if len(c) >= 2:
            return float(c[0]), float(c[1])
    if t == "MultiPoint":
        return _avg_centroid_from_rings(g.get("coordinates"))
    return None


def esri_polygon_centroid(geom: dict) -> Optional[tuple[float, float]]:
    rings = geom.get("rings") or []
    if not rings:
        return None
    return _avg_centroid_from_rings(rings)


# ---------------------------------------------------------------------------
# British National Grid (EPSG:27700) -> WGS84 fallback for Cadw rows that
# have null geometry but populated easting/northing. Implements the OS
# coordinate transformation (Helmert 7-parameter via OSGB36 -> WGS84). For
# our scale (centroid-of-polygon precision) the simpler approximate formulae
# from the OS guide are more than enough.
# ---------------------------------------------------------------------------
def bng_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    import math

    a = 6377563.396
    b = 6356256.910
    f0 = 0.9996012717
    n0 = -100000.0
    e0 = 400000.0
    phi0 = math.radians(49.0)
    lam0 = math.radians(-2.0)
    e2 = 1 - (b * b) / (a * a)
    n = (a - b) / (a + b)
    nn = n * n
    nnn = n * n * n

    phi = phi0
    M = 0.0
    while True:
        phi = (northing - n0 - M) / (a * f0) + phi
        M = (
            b * f0 * (
                (1 + n + 5 / 4 * nn + 5 / 4 * nnn) * (phi - phi0)
                - (3 * n + 3 * nn + 21 / 8 * nnn)
                * math.sin(phi - phi0) * math.cos(phi + phi0)
                + (15 / 8 * nn + 15 / 8 * nnn)
                * math.sin(2 * (phi - phi0)) * math.cos(2 * (phi + phi0))
                - (35 / 24 * nnn)
                * math.sin(3 * (phi - phi0)) * math.cos(3 * (phi + phi0))
            )
        )
        if abs(northing - n0 - M) < 1e-5:
            break

    sphi = math.sin(phi)
    cphi = math.cos(phi)
    nu = a * f0 / math.sqrt(1 - e2 * sphi * sphi)
    rho = a * f0 * (1 - e2) / (1 - e2 * sphi * sphi) ** 1.5
    eta2 = nu / rho - 1

    tphi = math.tan(phi)
    sec_phi = 1 / cphi
    VII = tphi / (2 * rho * nu)
    VIII = tphi / (24 * rho * nu ** 3) * (5 + 3 * tphi * tphi + eta2 - 9 * tphi * tphi * eta2)
    IX = tphi / (720 * rho * nu ** 5) * (61 + 90 * tphi * tphi + 45 * tphi ** 4)
    X = sec_phi / nu
    XI = sec_phi / (6 * nu ** 3) * (nu / rho + 2 * tphi * tphi)
    XII = sec_phi / (120 * nu ** 5) * (5 + 28 * tphi * tphi + 24 * tphi ** 4)
    XIIA = sec_phi / (5040 * nu ** 7) * (61 + 662 * tphi * tphi + 1320 * tphi ** 4 + 720 * tphi ** 6)

    de = easting - e0
    lat = phi - VII * de ** 2 + VIII * de ** 4 - IX * de ** 6
    lon = lam0 + X * de - XI * de ** 3 + XII * de ** 5 - XIIA * de ** 7

    return math.degrees(lon), math.degrees(lat)


# ---------------------------------------------------------------------------
# Cadw — WFS GeoJSON
# ---------------------------------------------------------------------------
@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=60), reraise=True)
def cadw_fetch(client: httpx.Client, start: int, count: int) -> dict[str, Any]:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": CADW_TYPENAME,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": str(count),
        "startIndex": str(start),
    }
    r = client.get(CADW_WFS, params=params)
    r.raise_for_status()
    return r.json()


def cadw_iter_features(client: httpx.Client, total_cap: int) -> Iterator[dict[str, Any]]:
    PAGE = 1000
    seen = 0
    start = 0
    while seen < total_cap:
        page = cadw_fetch(client, start, min(PAGE, total_cap - seen))
        feats = page.get("features", []) or []
        if not feats:
            return
        for f in feats:
            yield f
            seen += 1
            if seen >= total_cap:
                return
        start += len(feats)


def cadw_feature_to_row(feat: dict[str, Any]) -> Optional[dict[str, Any]]:
    p = feat.get("properties") or {}
    record_number = p.get("RecordNumber")
    if record_number is None:
        return None
    name = (p.get("Name") or "").strip() or None
    sam_number = (p.get("SAMNumber") or "").strip() or None
    site_type_hint = p.get("SiteType")
    period_hint = p.get("Period")
    site_type = classify_site_type(name, site_type_hint)
    if site_type == "archaeological_site" and site_type_hint:
        site_type_hint_lower = (site_type_hint or "").strip().lower()
        # Direct mapping of Cadw SiteType strings we don't otherwise pattern-match.
        explicit = {
            "standing stone": "standing_stone",
            "stone circle": "stone_circle",
            "round barrow": "round_barrow",
            "long barrow": "long_barrow",
            "barrow": "tumulus",
            "cairn": "cairn",
            "hillfort": "hillfort",
            "hill fort": "hillfort",
            "promontory fort": "hillfort",
            "settlement": "settlement",
            "deserted rural settlement": "settlement",
            "deserted medieval village": "settlement",
            "enclosure": "earthworks",
            "earthwork": "earthworks",
            "moat": "earthworks",
            "moated site": "earthworks",
            "castle": "castle",
            "motte": "castle",
            "motte and bailey": "castle",
            "abbey": "ruins",
            "priory": "ruins",
            "monastery": "ruins",
            "chapel": "ruins",
            "church": "ruins",
            "cross": "monument",
        }
        site_type = explicit.get(site_type_hint_lower, site_type)
    period = classify_period(name, period_hint) or (period_hint or None)

    geom = feat.get("geometry")
    cent = geojson_centroid(geom)
    if cent is None:
        e = p.get("easting")
        nn = p.get("northing")
        if e is not None and nn is not None:
            try:
                cent = bng_to_wgs84(float(e), float(nn))
            except Exception:
                cent = None
    if cent is None:
        return None
    lng, lat = cent

    # Sanity: Wales bbox roughly lat 51.3..53.5, lng -5.5..-2.6
    if not (-6.5 <= lng <= -2.0 and 51.0 <= lat <= 54.0):
        # Don't drop, but flag in tags. Most outliers will be data artifacts.
        pass

    osm_tags = {
        "RecordNumber": record_number,
        "SAMNumber": sam_number,
        "Name": name,
        "BroadClass": p.get("BroadClass"),
        "Period": period_hint,
        "SiteType": site_type_hint,
        "UnitaryAuthority": p.get("UnitaryAuthority"),
        "Community": p.get("Community"),
        "DesignationDate": p.get("DesignationDate"),
        "Report": p.get("Report"),
        "easting": p.get("easting"),
        "northing": p.get("northing"),
        "_source": "datamap_gov_wales_geoserver_wfs:inspire-wg:Cadw_SAM",
    }
    return {
        "osm_type": "node",
        "osm_id": int(record_number),
        "name": name,
        "site_type": site_type,
        "historic_period": period,
        "lat": float(lat),
        "lng": float(lng),
        "osm_tags": osm_tags,
    }


# ---------------------------------------------------------------------------
# HES — ArcGIS REST MapServer, paged by FID range
# ---------------------------------------------------------------------------
@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=60), reraise=True)
def hes_fetch_range(client: httpx.Client, fid_lo: int, fid_hi: int) -> dict[str, Any]:
    """Fetch FID in [fid_lo, fid_hi)."""
    params = {
        "where": f"FID >= {fid_lo} AND FID < {fid_hi}",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    r = client.get(f"{HES_LAYER}/query", params=params)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"ArcGIS error: {j['error']}")
    return j


def hes_count(client: httpx.Client) -> int:
    r = client.get(
        f"{HES_LAYER}/query",
        params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
    )
    r.raise_for_status()
    return int(r.json().get("count", 0))


def hes_object_id_max(client: httpx.Client) -> int:
    r = client.get(
        f"{HES_LAYER}/query",
        params={"where": "1=1", "returnIdsOnly": "true", "f": "json"},
    )
    r.raise_for_status()
    ids = r.json().get("objectIds", []) or []
    return max(ids) if ids else 0


def hes_iter_features(client: httpx.Client, total_cap: int) -> Iterator[dict[str, Any]]:
    fid_max = hes_object_id_max(client) + 1
    seen = 0
    fid = 0
    while fid < fid_max and seen < total_cap:
        page = hes_fetch_range(client, fid, fid + HES_PAGE_SIZE)
        feats = page.get("features", []) or []
        for f in feats:
            yield f
            seen += 1
            if seen >= total_cap:
                return
        fid += HES_PAGE_SIZE


_HES_ID_RE = re.compile(r"(?i)^\s*sm\s*0*([0-9]+)")


def hes_feature_to_row(feat: dict[str, Any]) -> Optional[dict[str, Any]]:
    a = feat.get("attributes") or {}
    inspire_id = a.get("inspireID") or a.get("InspireID") or a.get("INSPIREID") or ""
    m = _HES_ID_RE.match(str(inspire_id))
    if not m:
        return None
    sm_int = int(m.group(1))

    name = (a.get("siteName") or "").strip() or None
    site_type = classify_site_type(name)
    period = classify_period(name)

    geom = feat.get("geometry") or {}
    cent = esri_polygon_centroid(geom)
    if cent is None:
        return None
    lng, lat = cent

    osm_tags = {
        "inspireID": inspire_id,
        "siteName": name,
        "designation": a.get("designatio"),
        "designation_detail": a.get("designat_1"),
        "designation_status": a.get("designat_2"),
        "designation_subtype": a.get("designat_3"),
        "applicatio": a.get("applicatio"),
        "legalFound": a.get("legalFound"),
        "legalFou_1": a.get("legalFou_1"),
        "percentage": a.get("percentage"),
        "FID": a.get("FID"),
        "_source": "inspire.hes.scot:Scottish_Cultural_ProtectedSites/MapServer/3",
    }
    return {
        "osm_type": "node",
        "osm_id": sm_int + HES_ID_OFFSET,
        "name": name,
        "site_type": site_type,
        "historic_period": period,
        "lat": float(lat),
        "lng": float(lng),
        "osm_tags": osm_tags,
    }


# ---------------------------------------------------------------------------
# Insert — same pattern as historicengland_monuments.py
# ---------------------------------------------------------------------------
def upsert_batch(rows: list[dict[str, Any]], source_name: str) -> int:
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
                    "source": source_name,
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
# Per-source orchestration
# ---------------------------------------------------------------------------
def run_cadw(client: httpx.Client, cap: int, dry_run: bool) -> dict[str, Any]:
    print(f"\n=== Cadw (Wales) — cap {cap:,} ===")
    rows: list[dict[str, Any]] = []
    skipped = 0
    t0 = time.monotonic()
    for feat in cadw_iter_features(client, cap):
        row = cadw_feature_to_row(feat)
        if row:
            rows.append(row)
        else:
            skipped += 1
        if len(rows) and len(rows) % 1000 == 0:
            print(f"  cadw fetched {len(rows):,} ... ({time.monotonic()-t0:.1f}s)")
    print(f"Fetched {len(rows):,} Cadw rows in {time.monotonic()-t0:.1f}s "
          f"({skipped} skipped: no usable geometry).")
    return _summarise_and_insert(rows, "cadw", dry_run)


def run_hes(client: httpx.Client, cap: int, dry_run: bool) -> dict[str, Any]:
    print(f"\n=== HES (Scotland) — cap {cap:,} ===")
    total = hes_count(client)
    print(f"Server reports {total:,} HES Scheduled Monument records.")
    rows: list[dict[str, Any]] = []
    skipped = 0
    t0 = time.monotonic()
    for feat in hes_iter_features(client, cap):
        row = hes_feature_to_row(feat)
        if row:
            rows.append(row)
        else:
            skipped += 1
        if len(rows) and len(rows) % 1000 == 0:
            print(f"  hes fetched {len(rows):,} ... ({time.monotonic()-t0:.1f}s)")
    print(f"Fetched {len(rows):,} HES rows in {time.monotonic()-t0:.1f}s "
          f"({skipped} skipped).")
    return _summarise_and_insert(rows, "hes", dry_run)


def _summarise_and_insert(
    rows: list[dict[str, Any]], source_name: str, dry_run: bool
) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_period: dict[str, int] = {}
    for r in rows:
        by_type[r["site_type"]] = by_type.get(r["site_type"], 0) + 1
        if r["historic_period"]:
            by_period[r["historic_period"]] = by_period.get(r["historic_period"], 0) + 1

    print(f"\n  {source_name} by site_type:")
    for t, n in sorted(by_type.items(), key=lambda x: -x[1])[:10]:
        print(f"    {t:25s} {n:>6,}")
    print(f"\n  {source_name} by historic_period:")
    for p, n in sorted(by_period.items(), key=lambda x: -x[1])[:10]:
        print(f"    {p:25s} {n:>6,}")

    bbox = None
    if rows:
        lats = [r["lat"] for r in rows]
        lngs = [r["lng"] for r in rows]
        bbox = (min(lats), max(lats), min(lngs), max(lngs))
        print(
            f"  {source_name} bbox: lat {bbox[0]:.4f}..{bbox[1]:.4f}  "
            f"lng {bbox[2]:.4f}..{bbox[3]:.4f}"
        )

    inserted = 0
    if not dry_run and rows:
        print(f"\n  Inserting {len(rows):,} {source_name} rows ...")
        inserted = upsert_batch(rows, source_name)
        print(f"  Inserted (new {source_name} rows): {inserted:,}")

    return {
        "source": source_name,
        "fetched": len(rows),
        "inserted": inserted,
        "by_type": by_type,
        "bbox": bbox,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Cadw + HES Scheduled Monuments importer.")
    ap.add_argument(
        "--source",
        choices=("cadw", "hes", "both"),
        default="both",
        help="Which register to import.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Fetch + classify but do not insert.")
    ap.add_argument(
        "--limit",
        type=int,
        default=MAX_TOTAL,
        help=f"Cap rows fetched per source (default {MAX_TOTAL}).",
    )
    args = ap.parse_args()

    cap = min(args.limit, MAX_TOTAL)
    log.info("uk_extra.start", source=args.source, cap=cap, dry_run=args.dry_run)

    summaries: list[dict[str, Any]] = []
    with make_client() as client:
        if args.source in ("cadw", "both"):
            summaries.append(run_cadw(client, cap, args.dry_run))
        if args.source in ("hes", "both"):
            summaries.append(run_hes(client, cap, args.dry_run))

    print("\n=== Summary ===")
    for s in summaries:
        print(
            f"  {s['source']:6s}  fetched={s['fetched']:>6,}  inserted={s['inserted']:>6,}"
        )

    if not args.dry_run:
        print(
            "\nNow run: select crop_circles.cc_recompute_nearby_sites();  "
            "(left for the operator -- not invoked here)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
