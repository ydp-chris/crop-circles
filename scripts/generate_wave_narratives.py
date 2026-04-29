"""
generate_wave_narratives.py — One-off script to write Claude-authored narrative
paragraphs for the top cross-country crop-circle "wave days".

Reads:  crop_circles.cc_extra_stats() RPC -> wave_days[]
        crop_circles.formations (per-date detail)
Writes: crop_circles.wave_day_narratives (idempotent on wave_date)

Run:    .venv/bin/python scripts/generate_wave_narratives.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sibling db.py importable regardless of cwd.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# override=True to defeat any curly-quoted ANTHROPIC_API_KEY injected by the
# Claude Code session shell.
load_dotenv(ROOT / ".env", override=True)

from anthropic import Anthropic  # noqa: E402

from db import db  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
INPUT_PRICE_PER_MTOK = 0.80   # USD per 1M input tokens
OUTPUT_PRICE_PER_MTOK = 4.00  # USD per 1M output tokens
MAX_NARRATIVES = 8


def fetch_wave_days() -> list[dict]:
    """Top wave days with >=3 distinct countries, sorted desc by countries then count."""
    res = db.rpc("cc_extra_stats", {}).execute()
    payload = res.data or {}
    waves = payload.get("wave_days") or []
    # The RPC already sorts by n_countries desc, n desc; trust that order.
    return waves


def fetch_existing_dates() -> set[str]:
    res = db.table("wave_day_narratives").select("wave_date").execute()
    return {row["wave_date"] for row in (res.data or [])}


def fetch_formations_for_date(wave_date: str) -> list[dict]:
    res = (
        db.table("formations")
        .select("canonical_id, country, county, nearest_landmark, crop_type")
        .eq("event_date", wave_date)
        .order("country", desc=False)
        .order("county", desc=False)
        .order("canonical_id", desc=False)
        .execute()
    )
    return res.data or []


def build_prompt(wave_date: str, n_countries: int, countries: list[str], formations: list[dict]) -> str:
    lines = []
    countries_list = ", ".join(countries)
    for f in formations:
        cid = f.get("canonical_id") or "?"
        country = f.get("country") or "?"
        county = f.get("county") or "?"
        landmark = f.get("nearest_landmark") or "?"
        crop = f.get("crop_type") or "unknown"
        lines.append(f"- {cid} | {country}, {county}, {landmark} | crop: {crop}")
    formation_block = "\n".join(lines)

    return (
        f'Crop circle "wave day" narrative for {wave_date}.\n\n'
        f'On this date, {len(formations)} formations were documented across '
        f'{n_countries} countries: {countries_list}.\n\n'
        f'Per-formation details (sorted by country):\n{formation_block}\n\n'
        f'Write a 2-3 sentence narrative paragraph for a public research website. '
        f'Focus on what\'s notable: the geographic spread, any clustering of '
        f'locations, the crop variety, anything striking about the simultaneity. '
        f'Don\'t speculate about causation. Don\'t use the word "synchronicity" or '
        f'other woo. Don\'t speculate about extraterrestrials or hoaxers. State the '
        f'facts crisply, mention the most prominent location/landmark if any, and '
        f'let the reader draw their own inference. 60-100 words. No headlines, no '
        f'bullets, just prose.'
    )


def call_claude(client: Anthropic, prompt: str) -> tuple[str, float, dict]:
    msg = client.messages.create(
        model=MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()
    in_tok = msg.usage.input_tokens
    out_tok = msg.usage.output_tokens
    cost = (in_tok / 1_000_000) * INPUT_PRICE_PER_MTOK + (out_tok / 1_000_000) * OUTPUT_PRICE_PER_MTOK
    return text, cost, {"input_tokens": in_tok, "output_tokens": out_tok}


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    client = Anthropic()

    waves = fetch_wave_days()
    if not waves:
        print("No wave days returned by cc_extra_stats(); nothing to do.")
        return 0

    existing = fetch_existing_dates()
    print(f"Found {len(waves)} wave days; {len(existing)} already narrated.")

    written = 0
    total_cost = 0.0
    skipped_no_formations: list[str] = []
    refusals: list[str] = []

    for wave in waves:
        if written >= MAX_NARRATIVES:
            break

        wave_date = wave["date"]
        n_countries = wave["n_countries"]
        n = wave["n"]
        countries = wave.get("countries") or []

        if wave_date in existing:
            print(f"  skip {wave_date} (already narrated)")
            continue

        formations = fetch_formations_for_date(wave_date)
        if not formations:
            print(f"  skip {wave_date} (no formation rows found)")
            skipped_no_formations.append(wave_date)
            continue

        prompt = build_prompt(wave_date, n_countries, countries, formations)
        try:
            text, cost, usage = call_claude(client, prompt)
        except Exception as e:
            print(f"  ERROR calling Claude for {wave_date}: {e}", file=sys.stderr)
            continue

        if not text or len(text) < 30:
            print(f"  WARN: short/empty response for {wave_date}: {text!r}")
            refusals.append(wave_date)
            continue

        row = {
            "wave_date": wave_date,
            "n_countries": n_countries,
            "n_formations": n,
            "countries": countries,
            "narrative": text,
            "model": MODEL,
            "cost_usd": round(cost, 6),
        }
        db.table("wave_day_narratives").insert(row).execute()
        total_cost += cost
        written += 1
        print(
            f"  wrote {wave_date} | {n_countries}c/{n}f | "
            f"{usage['input_tokens']}in/{usage['output_tokens']}out | ${cost:.5f}"
        )

    print()
    print(f"Wrote {written} narratives, total cost ${total_cost:.4f}")
    if skipped_no_formations:
        print(f"  Skipped (no formation rows): {skipped_no_formations}")
    if refusals:
        print(f"  Refusals/short responses: {refusals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
