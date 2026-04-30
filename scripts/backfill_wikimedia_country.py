"""
backfill_wikimedia_country.py - Reverse-geocode Wikimedia formations that have
EXIF GPS but no country field, so they become eligible for cross-archive
dedup. ~30 candidates expected; Nominatim reverse-geocode at 1 req/sec.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from supabase import create_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(override=True)

URL = os.environ.get("SUPABASE_URL") or os.environ["SUPABASE_VS_URL"]
KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_VS_SERVICE_KEY"]
client = create_client(URL, KEY)
db = client.schema("crop_circles")

UA = "YDP-CropCircles/0.1 (https://github.com/YDP-Chris; fordchristopheralan@gmail.com)"
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"


def main() -> int:
    # Find Wikimedia formations with location set but no country
    res = (
        db.table("formations")
        .select("id, canonical_id, lat, lng, country, county")
        .is_("country", "null")
        .not_.is_("lat", "null")
        .execute()
    )
    candidates = [r for r in (res.data or []) if r.get("lat") is not None and r.get("lng") is not None]
    print(f"Found {len(candidates)} formations with coords but no country")

    updated = 0
    failed = 0
    with httpx.Client(timeout=20.0, headers={"User-Agent": UA}) as client_h:
        for f in candidates:
            params = {
                "lat": str(f["lat"]),
                "lon": str(f["lng"]),
                "format": "json",
                "zoom": "10",
                "addressdetails": "1",
            }
            try:
                r = client_h.get(NOMINATIM, params=params)
                r.raise_for_status()
                data = r.json()
                addr = data.get("address") or {}
                cc = (addr.get("country_code") or "").upper()
                county = (
                    addr.get("county")
                    or addr.get("state_district")
                    or addr.get("region")
                    or addr.get("state")
                )
                if not cc:
                    print(f"  no country for {f['canonical_id']}")
                    failed += 1
                else:
                    upd = {"country": cc}
                    if county and not f.get("county"):
                        upd["county"] = county
                    db.table("formations").update(upd).eq("id", f["id"]).execute()
                    print(f"  {f['canonical_id']}  -> {cc}{f' / {county}' if county else ''}")
                    updated += 1
            except Exception as e:
                print(f"  FAILED {f['canonical_id']}: {e}")
                failed += 1
            time.sleep(1.1)  # Nominatim policy: 1 req/sec

    print(f"\nDone: updated {updated}, failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
