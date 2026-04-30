"""
parse_os_grid.py - Find OS Grid references in formation notes and convert
to precise WGS84 coordinates. Temporary Temples post-2022 entries embed
grid refs like "OS Grid: SU 12392 62177" in the notes field; these resolve
to ~5m accuracy, vastly better than the ~500m landmark resolution.

Pure-Python BNG (EPSG:27700) -> WGS84 (EPSG:4326) via pyproj if available;
falls back to a manual Helmert transform if pyproj isn't installed.

Usage:
    .venv/bin/python scripts/parse_os_grid.py --dry-run
    .venv/bin/python scripts/parse_os_grid.py
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)


SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ["SUPABASE_VS_URL"]
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ["SUPABASE_VS_SERVICE_KEY"]
)
client = create_client(SUPABASE_URL, SUPABASE_KEY)
db = client.schema("crop_circles")

# OS Grid 100km square decoder. Two-letter scheme:
# First letter selects a 500km block; second letter selects 100km square within.
# Both letters use the same 5x5 letter grid (alphabet, 'I' skipped):
#
#   V W X Y Z   <-- north
#   Q R S T U
#   L M N O P
#   F G H J K
#   A B C D E   <-- south
#
# Of the 25 first-letter possibilities, only six 500km blocks fall over Britain:
# S/T (south UK), N/O (Scotland), H/J (Shetland). Their SW corner positions
# in EPSG:27700 are well-known.
FIRST_LETTER_500KM = {
    "S": (0, 0),
    "T": (500_000, 0),
    "N": (0, 500_000),
    "O": (500_000, 500_000),
    "H": (0, 1_000_000),
    "J": (500_000, 1_000_000),
}

_GRID_5X5 = [
    "ABCDE",  # top row of each 500km block, NORTH
    "FGHJK",
    "LMNOP",
    "QRSTU",
    "VWXYZ",  # bottom row, SOUTH
]


def _letter_to_col_row(letter: str) -> tuple[int, int]:
    for row_idx, row_str in enumerate(_GRID_5X5):
        if letter in row_str:
            return row_str.index(letter), row_idx
    raise ValueError(f"Bad OS grid letter: {letter!r}")


def grid_letters_to_offsets(letters: str) -> tuple[int, int]:
    """Two-letter OS grid square -> (easting_offset, northing_offset) in metres
    relative to the SW corner of the BNG (EPSG:27700)."""
    if len(letters) != 2:
        raise ValueError(f"Bad grid letters: {letters!r}")
    first, second = letters.upper()
    if first not in FIRST_LETTER_500KM:
        raise ValueError(f"First letter {first!r} not over Britain")
    base_e, base_n = FIRST_LETTER_500KM[first]
    col, row = _letter_to_col_row(second)
    # row 0 (top) is northernmost, row 4 (bottom) is southernmost.
    # Easting grows left-to-right, northing grows bottom-to-top.
    e = base_e + col * 100_000
    n = base_n + (4 - row) * 100_000
    return e, n


GRID_RE = re.compile(
    r"OS\s*Grid[:\s]+([A-Z]{2})\s+(\d{3,5})\s+(\d{3,5})",
    re.IGNORECASE,
)


def parse_grid_ref(text: str) -> tuple[int, int] | None:
    """Find first OS grid ref in text, return (easting, northing) in meters."""
    m = GRID_RE.search(text)
    if not m:
        return None
    letters = m.group(1).upper()
    e_str, n_str = m.group(2), m.group(3)
    try:
        e_offset, n_offset = grid_letters_to_offsets(letters)
    except ValueError:
        return None
    # Pad/truncate digit strings to 5 (1m precision)
    e_str = (e_str + "00000")[:5]
    n_str = (n_str + "00000")[:5]
    easting = e_offset + int(e_str)
    northing = n_offset + int(n_str)
    return easting, northing


def bng_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """British National Grid (EPSG:27700) -> WGS84 lat/lng (EPSG:4326)."""
    try:
        from pyproj import Transformer

        transformer = Transformer.from_crs(
            "EPSG:27700", "EPSG:4326", always_xy=True
        )
        lng, lat = transformer.transform(easting, northing)
        return lat, lng
    except ImportError:
        # Manual implementation: OSGB36 -> WGS84 via pure-python.
        # Source: Ordnance Survey "A guide to coordinate systems in Great Britain".
        # Step 1: BNG (E,N) -> OSGB36 ellipsoid lat/lng
        # Step 2: OSGB36 -> WGS84 via 7-parameter Helmert transform
        return _bng_to_wgs84_manual(easting, northing)


def _bng_to_wgs84_manual(E: float, N: float) -> tuple[float, float]:
    """Pure-Python BNG -> WGS84 fallback. Accuracy ~1-5m."""
    import math

    # Airy 1830 ellipsoid (used by OSGB36)
    a = 6377563.396
    b = 6356256.910
    e2 = 1 - (b * b) / (a * a)
    F0 = 0.9996012717  # central meridian scale factor
    phi0 = math.radians(49)
    lambda0 = math.radians(-2)
    N0 = -100000
    E0 = 400000
    n = (a - b) / (a + b)
    n2 = n * n
    n3 = n2 * n

    phi = phi0
    M = 0.0
    while abs(N - N0 - M) >= 0.00001:
        phi = (N - N0 - M) / (a * F0) + phi
        Ma = (1 + n + (5 / 4) * n2 + (5 / 4) * n3) * (phi - phi0)
        Mb = (3 * n + 3 * n * n + (21 / 8) * n3) * math.sin(phi - phi0) * math.cos(phi + phi0)
        Mc = ((15 / 8) * n2 + (15 / 8) * n3) * math.sin(2 * (phi - phi0)) * math.cos(2 * (phi + phi0))
        Md = (35 / 24) * n3 * math.sin(3 * (phi - phi0)) * math.cos(3 * (phi + phi0))
        M = b * F0 * (Ma - Mb + Mc - Md)

    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    nu = a * F0 / math.sqrt(1 - e2 * sin_phi * sin_phi)
    rho = a * F0 * (1 - e2) / pow(1 - e2 * sin_phi * sin_phi, 1.5)
    eta2 = nu / rho - 1

    tan_phi = math.tan(phi)
    sec_phi = 1 / cos_phi
    VII = tan_phi / (2 * rho * nu)
    VIII = tan_phi / (24 * rho * pow(nu, 3)) * (5 + 3 * tan_phi**2 + eta2 - 9 * tan_phi**2 * eta2)
    IX = tan_phi / (720 * rho * pow(nu, 5)) * (61 + 90 * tan_phi**2 + 45 * tan_phi**4)
    X = sec_phi / nu
    XI = sec_phi / (6 * pow(nu, 3)) * (nu / rho + 2 * tan_phi**2)
    XII = sec_phi / (120 * pow(nu, 5)) * (5 + 28 * tan_phi**2 + 24 * tan_phi**4)
    XIIA = sec_phi / (5040 * pow(nu, 7)) * (61 + 662 * tan_phi**2 + 1320 * tan_phi**4 + 720 * tan_phi**6)

    dE = E - E0
    lat_osgb = phi - VII * dE**2 + VIII * dE**4 - IX * dE**6
    lng_osgb = lambda0 + X * dE - XI * dE**3 + XII * dE**5 - XIIA * dE**7

    # OSGB36 -> WGS84 (7-parameter Helmert)
    # Convert to cartesian
    h = 0
    a_osgb = 6377563.396
    e2_osgb = e2
    nu_osgb = a_osgb / math.sqrt(1 - e2_osgb * math.sin(lat_osgb) ** 2)
    x1 = (nu_osgb + h) * math.cos(lat_osgb) * math.cos(lng_osgb)
    y1 = (nu_osgb + h) * math.cos(lat_osgb) * math.sin(lng_osgb)
    z1 = ((1 - e2_osgb) * nu_osgb + h) * math.sin(lat_osgb)

    # OSGB36 -> WGS84 parameters
    tx, ty, tz = 446.448, -125.157, 542.060
    rx, ry, rz = 0.1502, 0.2470, 0.8421  # arc seconds
    s = -20.4894 * 1e-6  # ppm

    rx_rad = math.radians(rx / 3600)
    ry_rad = math.radians(ry / 3600)
    rz_rad = math.radians(rz / 3600)

    x2 = tx + (1 + s) * x1 + (-rz_rad) * y1 + ry_rad * z1
    y2 = ty + rz_rad * x1 + (1 + s) * y1 + (-rx_rad) * z1
    z2 = tz + (-ry_rad) * x1 + rx_rad * y1 + (1 + s) * z1

    # Convert WGS84 cartesian back to lat/lng
    a_wgs = 6378137.0
    b_wgs = 6356752.3142
    e2_wgs = 1 - (b_wgs * b_wgs) / (a_wgs * a_wgs)
    p = math.sqrt(x2 * x2 + y2 * y2)
    lat = math.atan2(z2, p * (1 - e2_wgs))
    for _ in range(8):
        nu_w = a_wgs / math.sqrt(1 - e2_wgs * math.sin(lat) ** 2)
        lat = math.atan2(z2 + e2_wgs * nu_w * math.sin(lat), p)
    lng = math.atan2(y2, x2)

    return math.degrees(lat), math.degrees(lng)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    # Pull all formations whose notes contain "OS Grid:" — Temporary Temples is the
    # main source but anything with that pattern is fair game.
    res = (
        db.table("formations")
        .select("id, canonical_id, notes, location_precision_m")
        .ilike("notes", "%OS Grid%")
        .execute()
    )
    rows = res.data or []
    if args.limit:
        rows = rows[: args.limit]

    print(f"Found {len(rows)} formations with OS Grid in notes")

    parsed = 0
    updated = 0
    failed = 0
    for f in rows:
        result = parse_grid_ref(f["notes"] or "")
        if not result:
            failed += 1
            continue
        easting, northing = result
        try:
            lat, lng = bng_to_wgs84(easting, northing)
        except Exception as e:
            print(f"  CONVERSION FAILED for {f['canonical_id']}: {e}")
            failed += 1
            continue
        parsed += 1
        if args.dry_run:
            print(f"  {f['canonical_id']}  E={easting} N={northing} -> lat={lat:.6f} lng={lng:.6f}")
            continue

        # Update via the existing RPC + set precision
        db.rpc(
            "cc_set_formation_location",
            {"p_formation_id": f["id"], "p_lat": lat, "p_lng": lng},
        ).execute()
        db.table("formations").update({"location_precision_m": 10}).eq("id", f["id"]).execute()
        updated += 1

    print(f"\nDone: parsed {parsed}, updated {updated}, failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
