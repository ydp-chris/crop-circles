"""
extract_encodings.py — One-shot extraction of "decoded message" claims about
crop circles from source_records into crop_circles.formation_encodings.

Strategy:
  1. Pull source_records that pass a cheap keyword pre-filter (pi/arecibo/
     binary/ascii/mandelbrot/fractal/julia/euler/code/decoded/message/cipher/
     sacred geometry/planetary/etc).
  2. Strip HTML, normalize whitespace, cap at ~6000 chars.
  3. Send to Claude Haiku 4.5 with a strict structured prompt asking for
     specific decoded-message claims only — not generic topical mentions.
  4. Each claim becomes a row in formation_encodings, joined to the formation
     via formation_aliases (source_id + source_record_id).

Idempotent: before insert, we query existing rows for the (formation_id,
encoding_type) pair and skip if a row with sufficiently similar decoded_text
already exists (the schema lacks a unique constraint, so we enforce in code).

Run:
    .venv/bin/python scripts/extract_encodings.py --dry-run
    .venv/bin/python scripts/extract_encodings.py --limit 50
    .venv/bin/python scripts/extract_encodings.py --source pringle
    .venv/bin/python scripts/extract_encodings.py            # live, all sources

Cost discipline:
  - Hard cost cap of $5.00 (COST_ABORT_USD).
  - Print running cost every 50 LLM calls.
  - Aborts cleanly when the cap is exceeded.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

# override=True to defeat curly-quoted ANTHROPIC_API_KEY injected by the
# parent shell environment (Claude Code session smart-quotes bug).
load_dotenv(ROOT / ".env", override=True)

from anthropic import Anthropic  # noqa: E402

from db import db  # noqa: E402


# ============================================================================
# Configuration
# ============================================================================
MODEL = "claude-haiku-4-5-20251001"
INPUT_PRICE_PER_MTOK = 0.80   # USD per 1M input tokens
OUTPUT_PRICE_PER_MTOK = 4.00  # USD per 1M output tokens
COST_ABORT_USD = 5.00
EXCERPT_CAP_CHARS = 6000
PROGRESS_EVERY = 50

VALID_ENCODING_TYPES = {
    "binary_ascii", "pi", "eulers_identity", "arecibo_response",
    "planetary_alignment", "fractal", "sacred_geometry", "unknown", "other",
}
VALID_ACCEPTANCE = {
    "verified", "plausible", "contested", "fringe", "disputed",
}

# Cheap keyword pre-filter — only records whose raw_html contains at least
# one of these (case-insensitive substring) will be considered. Tuned to the
# vocabulary actually present in the Pringle / CCC archives.
PREFILTER_KEYWORDS = [
    "arecibo", "binary", "ascii", "mandelbrot", "fractal", "julia",
    "euler", "decoded", "message", "cipher", "sacred geometry",
    "planetary", "digit", "encode", "equation", "mathematical",
    "crabwood", "chilbolton",
]

# Sources that contain narrative HTML worth extracting from.
# (CCC photo pages have only ~380 chars of boilerplate, but the rare ones
# that do mention an encoding are still worth processing.)
DEFAULT_SOURCE_SLUGS = ["pringle", "cropcirclecenter"]


# ============================================================================
# Prompt
# ============================================================================
PROMPT_TEMPLATE = """You are extracting "decoded message" claims about crop circles from archival text. The crop circle community has long claimed specific formations encode mathematical or symbolic messages: pi (the digits), Arecibo radio message responses, Euler's identity, planetary alignments, Mandelbrot/Julia fractals, sacred geometry patterns, or binary ASCII text.

Below is an excerpt from {source_title} ({source_url}).

EXCERPT:
\"\"\"
{text}
\"\"\"

Question: Does this excerpt describe a SPECIFIC decoded-message claim about a crop circle? Distinguish:
- A specific decoded claim ("the formation at X encodes the first 10 digits of pi as decoded by researcher Y") = YES, extract it
- General topical mention ("some say crop circles contain coded messages") = NO
- Description of geometry without a decoding claim ("the formation has fivefold symmetry") = NO
- Decoder's name/method should be extracted only if explicitly attributed in the text

Respond ONLY in this exact JSON shape, no markdown:

{{
  "claims": [
    {{
      "encoding_type": "binary_ascii" or "pi" or "eulers_identity" or "arecibo_response" or "planetary_alignment" or "fractal" or "sacred_geometry" or "unknown" or "other",
      "decoded_text": "what the formation supposedly says/represents (verbatim or paraphrase)",
      "decoder_name": "Vigay" / "Reed" / "Treurniet" / etc, or null if not stated",
      "decoder_method": "brief method description, or null",
      "decoder_confidence": 0.0-1.0 (the extractor's confidence the claim is genuinely in the text, not the claim's truth),
      "community_acceptance": "verified" or "plausible" or "contested" or "fringe" or "disputed" - based on how the text characterizes acceptance,
      "source_citation": "the source field cited if any, else null",
      "notes": "any caveats / nuance, or null"
    }}
  ]
}}

If no claim, return {{"claims": []}}."""


# ============================================================================
# Helpers
# ============================================================================
def strip_html(html: str) -> str:
    """Conservative HTML strip: drop scripts/styles, then tags, then collapse ws."""
    if not html:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common entities cheaply
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'")
            .replace("&apos;", "'"))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def excerpt_for_llm(html_or_text: str) -> str:
    text = strip_html(html_or_text)
    if len(text) > EXCERPT_CAP_CHARS:
        # Try to center the excerpt on the first keyword hit so we don't
        # waste tokens on boilerplate nav.
        lower = text.lower()
        first_hit = None
        for kw in PREFILTER_KEYWORDS:
            idx = lower.find(kw)
            if idx >= 0 and (first_hit is None or idx < first_hit):
                first_hit = idx
        if first_hit is not None:
            half = EXCERPT_CAP_CHARS // 2
            start = max(0, first_hit - half)
            end = start + EXCERPT_CAP_CHARS
            text = text[start:end]
        else:
            text = text[:EXCERPT_CAP_CHARS]
    return text


def passes_prefilter(html: str) -> bool:
    if not html:
        return False
    lower = html.lower()
    return any(kw in lower for kw in PREFILTER_KEYWORDS)


def parse_claim_json(raw: str) -> list[dict]:
    """Tolerant JSON parse — strip markdown fences if Haiku slipped them in."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        # Try to extract the first JSON object substring
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if not m:
            return []
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    return obj.get("claims") or []


def normalize_text_for_dedup(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip().lower()[:200]


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N source_records (post-prefilter).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run extraction but do not insert rows.")
    parser.add_argument("--source", type=str, default=None,
                        help="Restrict to a single source slug (e.g. 'pringle').")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Source(s) to scan
    # ------------------------------------------------------------------
    sources_res = db.table("sources").select("id,slug,name").execute()
    by_slug = {s["slug"]: s for s in sources_res.data}
    if args.source:
        if args.source not in by_slug:
            print(f"Unknown source slug: {args.source}", file=sys.stderr)
            return 2
        slugs = [args.source]
    else:
        slugs = [s for s in DEFAULT_SOURCE_SLUGS if s in by_slug]

    print(f"Scanning sources: {slugs}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"Cost cap: ${COST_ABORT_USD:.2f}")
    print()

    # ------------------------------------------------------------------
    # Collect candidate source_records (prefilter via ilike for each kw)
    # ------------------------------------------------------------------
    candidate_ids: set[str] = set()
    total_scanned = 0
    for slug in slugs:
        sid = by_slug[slug]["id"]
        # Total record count for reporting
        cres = (db.table("source_records").select("id", count="exact")
                .eq("source_id", sid).not_.is_("raw_html", "null").limit(1).execute())
        total_scanned += cres.count or 0
        for kw in PREFILTER_KEYWORDS:
            pat = f"%{kw}%"
            offset = 0
            page_size = 1000
            while True:
                res = (db.table("source_records").select("id")
                       .eq("source_id", sid).ilike("raw_html", pat)
                       .range(offset, offset + page_size - 1).execute())
                if not res.data:
                    break
                for r in res.data:
                    candidate_ids.add(r["id"])
                if len(res.data) < page_size:
                    break
                offset += page_size

    candidate_list = sorted(candidate_ids)
    if args.limit is not None:
        candidate_list = candidate_list[: args.limit]

    print(f"Records scanned (with raw_html): {total_scanned}")
    print(f"Candidates after prefilter: {len(candidate_ids)}")
    print(f"Processing: {len(candidate_list)}")
    print()

    if not candidate_list:
        print("Nothing to do.")
        return 0

    # ------------------------------------------------------------------
    # Anthropic client
    # ------------------------------------------------------------------
    client = Anthropic()

    total_input_tok = 0
    total_output_tok = 0
    llm_calls = 0
    rows_to_insert: list[dict] = []
    encoding_type_counter: Counter = Counter()
    skipped_no_alias = 0
    skipped_dup = 0
    extraction_errors = 0
    no_claim = 0

    # Pre-pull aliases for efficient lookup
    print("Loading aliases...")
    alias_map: dict[tuple[str, str], str] = {}  # (source_id, source_record_id) -> formation_id
    for slug in slugs:
        sid = by_slug[slug]["id"]
        offset = 0
        while True:
            res = (db.table("formation_aliases")
                   .select("source_id,source_record_id,formation_id")
                   .eq("source_id", sid)
                   .range(offset, offset + 999).execute())
            if not res.data:
                break
            for a in res.data:
                alias_map[(a["source_id"], a["source_record_id"])] = a["formation_id"]
            if len(res.data) < 1000:
                break
            offset += 1000
    print(f"Loaded {len(alias_map)} aliases.")
    print()

    # Existing-row cache for idempotency: (formation_id, encoding_type)
    # -> set of normalized decoded_text
    existing_cache: dict[tuple[str, str], set[str]] = {}

    def get_existing(formation_id: str, encoding_type: str) -> set[str]:
        key = (formation_id, encoding_type)
        if key not in existing_cache:
            res = (db.table("formation_encodings")
                   .select("decoded_text")
                   .eq("formation_id", formation_id)
                   .eq("encoding_type", encoding_type)
                   .execute())
            existing_cache[key] = {
                normalize_text_for_dedup(r.get("decoded_text"))
                for r in (res.data or [])
            }
        return existing_cache[key]

    # ------------------------------------------------------------------
    # Process candidates
    # ------------------------------------------------------------------
    t0 = time.time()
    for i, rec_id in enumerate(candidate_list, start=1):
        # Cost cap check
        cost = (total_input_tok / 1_000_000 * INPUT_PRICE_PER_MTOK
                + total_output_tok / 1_000_000 * OUTPUT_PRICE_PER_MTOK)
        if cost >= COST_ABORT_USD:
            print(f"\n>>> Cost cap reached (${cost:.4f}); aborting at record {i}.")
            break

        rec_res = (db.table("source_records")
                   .select("id,source_id,source_record_id,source_url,raw_html")
                   .eq("id", rec_id).limit(1).execute())
        if not rec_res.data:
            continue
        rec = rec_res.data[0]

        formation_id = alias_map.get((rec["source_id"], rec["source_record_id"]))
        if not formation_id:
            skipped_no_alias += 1
            continue

        text = excerpt_for_llm(rec["raw_html"] or "")
        if len(text) < 100:
            continue

        # Source title is the slug name
        slug_for_rec = next((s for s in slugs
                             if by_slug[s]["id"] == rec["source_id"]), "?")
        source_title = by_slug[slug_for_rec]["name"]

        prompt = PROMPT_TEMPLATE.format(
            source_title=source_title,
            source_url=rec.get("source_url") or "",
            text=text,
        )

        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            extraction_errors += 1
            print(f"  [{i}] LLM error on {rec['source_record_id']}: {e}")
            time.sleep(1.0)
            continue

        llm_calls += 1
        total_input_tok += resp.usage.input_tokens
        total_output_tok += resp.usage.output_tokens

        raw_text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        claims = parse_claim_json(raw_text)
        if not claims:
            no_claim += 1
        for claim in claims:
            etype = claim.get("encoding_type")
            if etype not in VALID_ENCODING_TYPES:
                etype = "other"
            decoded_text = (claim.get("decoded_text") or "").strip() or None
            acceptance = claim.get("community_acceptance")
            if acceptance not in VALID_ACCEPTANCE:
                acceptance = None
            conf_raw = claim.get("decoder_confidence")
            try:
                conf = float(conf_raw) if conf_raw is not None else None
                if conf is not None:
                    conf = max(0.0, min(1.0, conf))
            except (TypeError, ValueError):
                conf = None

            # Idempotency: skip if formation already has a similar decoded_text
            existing = get_existing(formation_id, etype)
            norm = normalize_text_for_dedup(decoded_text)
            if norm and norm in existing:
                skipped_dup += 1
                continue

            row = {
                "formation_id": formation_id,
                "encoding_type": etype,
                "decoded_text": decoded_text,
                "decoder_name": claim.get("decoder_name") or None,
                "decoder_method": claim.get("decoder_method") or None,
                "decoder_confidence": conf,
                "community_acceptance": acceptance,
                "source_citation": (claim.get("source_citation")
                                    or rec.get("source_url")) or None,
                "notes": claim.get("notes") or None,
            }
            rows_to_insert.append(row)
            encoding_type_counter[etype] += 1
            # Add to local cache so subsequent claims in same run dedup too
            if norm:
                existing.add(norm)

        if llm_calls % PROGRESS_EVERY == 0:
            cost = (total_input_tok / 1_000_000 * INPUT_PRICE_PER_MTOK
                    + total_output_tok / 1_000_000 * OUTPUT_PRICE_PER_MTOK)
            elapsed = time.time() - t0
            print(f"  [{llm_calls} calls / {i} records] "
                  f"in_tok={total_input_tok} out_tok={total_output_tok} "
                  f"cost=${cost:.4f} claims={len(rows_to_insert)} "
                  f"elapsed={elapsed:.0f}s")

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------
    inserted = 0
    if rows_to_insert and not args.dry_run:
        # Insert in chunks to be polite to PostgREST
        CHUNK = 50
        for j in range(0, len(rows_to_insert), CHUNK):
            batch = rows_to_insert[j:j + CHUNK]
            try:
                res = db.table("formation_encodings").insert(batch).execute()
                inserted += len(res.data or [])
            except Exception as e:
                print(f"  Insert error on chunk {j}: {e}")
                # Try one-by-one to salvage what we can
                for row in batch:
                    try:
                        r = db.table("formation_encodings").insert(row).execute()
                        if r.data:
                            inserted += len(r.data)
                    except Exception as e2:
                        print(f"  - row failed: {e2}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    cost = (total_input_tok / 1_000_000 * INPUT_PRICE_PER_MTOK
            + total_output_tok / 1_000_000 * OUTPUT_PRICE_PER_MTOK)
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Records scanned (with raw_html): {total_scanned}")
    print(f"Candidates after prefilter:      {len(candidate_ids)}")
    print(f"Processed:                       {len(candidate_list)}")
    print(f"LLM calls:                       {llm_calls}")
    print(f"  Input tokens:                  {total_input_tok}")
    print(f"  Output tokens:                 {total_output_tok}")
    print(f"  Total cost:                    ${cost:.4f}")
    print()
    print(f"Skipped (no alias):              {skipped_no_alias}")
    print(f"Skipped (duplicate):             {skipped_dup}")
    print(f"No claim found:                  {no_claim}")
    print(f"Extraction errors:               {extraction_errors}")
    print()
    print(f"Claims extracted:                {len(rows_to_insert)}")
    print(f"Rows inserted:                   {inserted}"
          + ("  (DRY-RUN — nothing written)" if args.dry_run else ""))
    print()
    print("Breakdown by encoding_type:")
    for etype, n in encoding_type_counter.most_common():
        print(f"  {etype:24s} {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
