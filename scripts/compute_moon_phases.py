"""
compute_moon_phases.py — backfill lunar phase data for crop_circles.formations.

For every formation with a non-null event_date we compute:
  moon_phase       — illumination fraction at noon UTC (0.0..1.0)
  moon_phase_name  — one of 8 phase bins, ~3.69 days each, derived from the
                     synodic age (days since previous new moon).

The 8-bin centered scheme aligns the cardinal phases (new, first quarter,
full, last quarter) with the *centers* of their bins, so the bin edges sit
at (1/16, 3/16, 5/16, 7/16, 9/16, 11/16, 13/16, 15/16) of the synodic
period (~29.5306 days). This matches what the crop-circle community
typically means by "on the full moon" — within ~1.85 days of exact full.

Note on dotenv: Claude Code injects a curly-quoted ANTHROPIC_API_KEY into
the shell that breaks downstream code. We use load_dotenv(override=True)
so the local .env wins. (Not relevant here since we don't call Anthropic,
but the pattern matters across the codebase.)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, time, timezone
from pathlib import Path

import ephem
from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

SYNODIC_DAYS = 29.530588853
PHASE_NAMES = [
    "new",
    "waxing_crescent",
    "first_quarter",
    "waxing_gibbous",
    "full",
    "waning_gibbous",
    "last_quarter",
    "waning_crescent",
]


def phase_for_date(d) -> tuple[float, str]:
    """
    Returns (illumination_fraction, phase_name) for noon-UTC on date d.

    illumination_fraction is ephem.Moon.phase / 100, so 0.0=new, 1.0=full.
    phase_name uses an 8-bin centered scheme based on synodic age, where the
    'full' bin is centered on the moment of full moon and spans ~3.69 days.
    """
    dt = datetime.combine(d, time(12, 0, tzinfo=timezone.utc))
    obs_date = ephem.Date(dt)

    moon = ephem.Moon()
    moon.compute(obs_date)
    illum = float(moon.phase) / 100.0

    prev_new = ephem.previous_new_moon(obs_date)
    age_days = float(obs_date - prev_new)
    # Normalize age into [0, SYNODIC_DAYS) — ephem can occasionally hand back
    # an age slightly above the period or just below zero near transitions.
    age_days = age_days % SYNODIC_DAYS

    # 8-bin centered: bin k covers [(k - 0.5)/8, (k + 0.5)/8) of the cycle.
    # Shift by half a bin so 'new' (k=0) is centered on age=0.
    frac = age_days / SYNODIC_DAYS
    shifted = (frac + 1.0 / 16.0) % 1.0
    bin_idx = int(shifted * 8)
    if bin_idx == 8:
        bin_idx = 0
    return round(illum, 6), PHASE_NAMES[bin_idx]


def main() -> int:
    client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    db = client.schema("crop_circles")

    # Pull all formations that have an event_date. Page through to avoid
    # PostgREST default row caps.
    page = 1000
    offset = 0
    rows: list[dict] = []
    while True:
        res = (
            db.table("formations")
            .select("id,event_date")
            .not_.is_("event_date", "null")
            .order("event_date")
            .range(offset, offset + page - 1)
            .execute()
        )
        chunk = res.data or []
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    print(f"Fetched {len(rows)} formations with event_date.")

    updated = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        try:
            d = datetime.fromisoformat(row["event_date"]).date()
        except Exception as exc:
            print(f"  skip {row['id']}: bad date {row.get('event_date')!r} ({exc})")
            failed += 1
            continue

        illum, name = phase_for_date(d)

        try:
            (
                db.table("formations")
                .update({"moon_phase": illum, "moon_phase_name": name})
                .eq("id", row["id"])
                .execute()
            )
            updated += 1
        except Exception as exc:
            print(f"  update failed {row['id']}: {exc}")
            failed += 1

        if i % 200 == 0:
            print(f"  ... {i}/{len(rows)} ({updated} ok, {failed} fail)")

    print(f"\nDone. updated={updated} failed={failed} total={len(rows)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
