"""
extract_historical.py — One-shot ingest of pre-photographic crop-circle text
references into crop_circles.historical_records.

Pulls 3-4 public-domain works, chunks them, asks Claude Haiku to identify
discrete event-like passages resembling crop circles ("fairy ring" of laid
corn, geometric flattening of standing crop, the Mowing-Devil tale, etc.),
and inserts each match as a row in historical_records.

Run:
    .venv/bin/python scripts/extract_historical.py
    .venv/bin/python scripts/extract_historical.py --source mowing-devil-1678

Idempotent: re-running upserts on (text_source, excerpt_hash). The hash is a
generated column server-side, so we omit it from the payload.

Cost discipline:
- Hard-cap of 100 LLM calls across all sources.
- Skip chunks shorter than 200 chars.
- Print running cost every 10 chunks; abort if cost exceeds $5.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# Make sibling db.py importable regardless of cwd.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# override=True to defeat any curly-quoted ANTHROPIC_API_KEY injected by the
# Claude Code session shell into the parent environment.
load_dotenv(ROOT / ".env", override=True)

import httpx  # noqa: E402
from anthropic import Anthropic  # noqa: E402

from db import db  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
INPUT_PRICE_PER_MTOK = 0.80   # USD per 1M input tokens
OUTPUT_PRICE_PER_MTOK = 4.00  # USD per 1M output tokens

# Cost discipline
MAX_LLM_CALLS = 100
COST_ABORT_USD = 5.00
MIN_CHUNK_CHARS = 200
INPUT_CAP_CHARS = 200_000
CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200

UA = "CropCirclesResearchBot/1.0 (research; fordchristopheralan@gmail.com)"

# Cheap pre-filter: for sources with a `prefilter_keywords` set, only chunks
# containing one of these (case-insensitive) get sent to Claude. This is for
# books that mostly aren't about crop circles (e.g. Fort's anomalies grab-bag),
# so we don't burn 100 calls on rains-of-frogs paragraphs.
PREFILTER_KEYWORDS = {
    "field", "crop", "wheat", "oats", "barley", "corn", "harvest",
    "circle", "ring", "rings", "fairy", "flatten", "laid", "trampl",
    "mow", "mown", "mowing", "stalks", "grain", "meadow",
}


# ===========================================================================
# Source registry
# ===========================================================================
@dataclass
class Source:
    slug: str
    text_source: str       # short citation like "Mowing-Devil 1678"
    text_source_url: str
    publication_year: int
    fetcher: str           # 'gutenberg-txt' | 'archive-djvu' | 'wikipedia-section'
    fetch_arg: str         # ID, URL, or page name
    # Optional regex window: if set, after fetch we slice to first match window
    # to avoid spending tokens on irrelevant chapters. (start_kw, end_kw, pad_after)
    window: Optional[tuple[str, str, int]] = None
    # If True, only send chunks containing PREFILTER_KEYWORDS to Claude. Use for
    # sources like Fort's anomalies grab-bag where most of the text is unrelated.
    prefilter: bool = False


SOURCES: list[Source] = [
    Source(
        slug="mowing-devil-1678",
        text_source="Mowing-Devil 1678",
        text_source_url="https://en.wikipedia.org/wiki/Mowing-Devil",
        publication_year=1678,
        fetcher="wikipedia-section",
        fetch_arg="Mowing-Devil",
    ),
    Source(
        slug="plot-1686",
        text_source="Plot 1686",
        text_source_url=(
            "https://archive.org/details/"
            "bim_early-english-books-1641-1700_the-natural-history-of-s_plot-robert_1686"
        ),
        publication_year=1686,
        fetcher="archive-djvu",
        fetch_arg=(
            "bim_early-english-books-1641-1700_the-natural-history-of-s_plot-robert_1686"
        ),
        # Plot's fairy-ring chapter starts at "Fairy cir(cles)". Take ~50K
        # chars from there forward to cover the whole chapter without paying
        # for his unrelated geology / mineralogy chapters.
        window=("Fairy cir", "", 50_000),
    ),
    Source(
        slug="fort-damned-1919",
        text_source="Fort 1919",
        text_source_url="https://www.gutenberg.org/ebooks/22472",
        publication_year=1919,
        fetcher="gutenberg-txt",
        fetch_arg="22472",
        prefilter=True,
    ),
    Source(
        slug="fort-newlands-1923",
        text_source="Fort 1923",
        text_source_url="https://www.gutenberg.org/ebooks/4067",
        publication_year=1923,
        fetcher="gutenberg-txt",
        fetch_arg="4067",
        prefilter=True,
    ),
]


# ===========================================================================
# Fetchers
# ===========================================================================
def fetch_text(source: Source, client: httpx.Client) -> Optional[str]:
    try:
        if source.fetcher == "gutenberg-txt":
            url = f"https://www.gutenberg.org/cache/epub/{source.fetch_arg}/pg{source.fetch_arg}.txt"
            r = client.get(url, follow_redirects=True, timeout=30.0)
            r.raise_for_status()
            text = r.text
            # Strip Gutenberg header/footer if present.
            m = re.search(r"\*\*\*\s*START OF.*?\*\*\*", text, re.I | re.S)
            if m:
                text = text[m.end():]
            m = re.search(r"\*\*\*\s*END OF.*?\*\*\*", text, re.I | re.S)
            if m:
                text = text[:m.start()]
            return text

        if source.fetcher == "archive-djvu":
            url = (
                f"https://archive.org/download/{source.fetch_arg}/"
                f"{source.fetch_arg}_djvu.txt"
            )
            r = client.get(url, follow_redirects=True, timeout=60.0)
            r.raise_for_status()
            text = r.text
            # Plot uses long-s "ſ"; normalize so Claude isn't burning tokens on
            # Unicode oddities and so our keyword matching stays sane.
            text = text.replace("ſ", "s")
            return text

        if source.fetcher == "wikipedia-section":
            url = (
                f"https://en.wikipedia.org/w/api.php?action=parse&format=json&"
                f"prop=wikitext&page={source.fetch_arg}"
            )
            r = client.get(url, follow_redirects=True, timeout=20.0)
            r.raise_for_status()
            wikitext = r.json()["parse"]["wikitext"]["*"]
            # The pamphlet text lives inside a {{quote|...}} template in the
            # Transcription section. Extract that block; fall back to whole page.
            m = re.search(r"==\s*Transcription\s*==(.*?)(?:==|\Z)", wikitext, re.S)
            section = m.group(1) if m else wikitext
            # Strip wiki templates we don't care about, keep prose-ish text.
            section = re.sub(r"\{\{cite[^}]*\}\}", " ", section, flags=re.I | re.S)
            section = re.sub(r"<ref[^>]*>.*?</ref>", " ", section, flags=re.I | re.S)
            section = re.sub(r"<ref[^/]*/>", " ", section, flags=re.I | re.S)
            section = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", section)
            section = re.sub(r"'''?", "", section)
            section = section.replace("ſ", "s")
            # Strip the {{quote| wrapper but keep its content.
            section = re.sub(r"\{\{quote\|", " ", section, flags=re.I)
            section = re.sub(r"\}\}", " ", section)
            section = re.sub(r"\n{3,}", "\n\n", section).strip()
            return section
    except Exception as e:
        print(f"  FETCH FAIL [{source.slug}]: {e}", file=sys.stderr)
        return None

    return None


def apply_window(text: str, window: Optional[tuple[str, str, int]]) -> str:
    if not window:
        return text
    start_kw, end_kw, pad_after = window
    start_idx = 0
    if start_kw:
        m = re.search(re.escape(start_kw), text, re.I)
        if m:
            start_idx = max(0, m.start() - 200)
    end_idx = len(text)
    if end_kw:
        m = re.search(re.escape(end_kw), text[start_idx:], re.I)
        if m:
            end_idx = start_idx + m.end()
    if pad_after:
        end_idx = min(len(text), start_idx + pad_after)
    return text[start_idx:end_idx]


# ===========================================================================
# Chunking
# ===========================================================================
def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if not text:
        return []
    out = []
    i = 0
    n = len(text)
    while i < n:
        out.append(text[i:i + size])
        if i + size >= n:
            break
        i += size - overlap
    return out


# ===========================================================================
# LLM call
# ===========================================================================
PROMPT_TEMPLATE = """Below is a passage from {text_source} ({publication_year}).

PASSAGE:
\"\"\"
{chunk}
\"\"\"

Question: Does this passage describe a discrete real-world event resembling a crop circle - that is, a geometric flattening of standing crop in a field (or a closely related anomaly like a "fairy ring" explicitly described as flattened/laid crop)?

Strict instructions:
- Only respond yes if the passage describes a SPECIFIC event (or events) of this nature, not general theoretical discussion or causation debate.
- Distinguish from generic agricultural reports of windflattening / lodging - those are NOT crop-circle-like.
- A "fairy ring" of mushrooms is NOT a crop circle. A "fairy ring" of laid corn IS.
- Devil/witch attribution is fine; describe-without-judgment.

Respond ONLY in this exact JSON shape, no markdown:

{{
  "matches": [
    {{
      "excerpt": "verbatim 200-1500 char passage from the input that describes the event",
      "summary": "one sentence in modern English describing what was reported",
      "country": "GB" or "FR" or "?",
      "location_text": "place name as written, or null",
      "event_year_min": YYYY or null,
      "event_year_max": YYYY or null,
      "confidence": 0.0-1.0
    }}
  ]
}}

If no match in this passage, return {{"matches": []}}."""


def call_claude(client: Anthropic, source: Source, chunk: str) -> tuple[list[dict], float]:
    prompt = PROMPT_TEMPLATE.format(
        text_source=source.text_source,
        publication_year=source.publication_year,
        chunk=chunk,
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        b.text for b in msg.content if getattr(b, "type", None) == "text"
    ).strip()

    in_tok = msg.usage.input_tokens
    out_tok = msg.usage.output_tokens
    cost = (
        (in_tok / 1_000_000) * INPUT_PRICE_PER_MTOK
        + (out_tok / 1_000_000) * OUTPUT_PRICE_PER_MTOK
    )

    # Tolerate stray markdown fencing even though we asked for plain JSON.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        payload = json.loads(text)
        matches = payload.get("matches", []) or []
        if not isinstance(matches, list):
            matches = []
    except json.JSONDecodeError:
        # Try to find the first {...} object as a last-ditch parse.
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                payload = json.loads(m.group(0))
                matches = payload.get("matches", []) or []
            except json.JSONDecodeError:
                matches = []
        else:
            matches = []

    return matches, cost


# ===========================================================================
# Insert
# ===========================================================================
def coerce_year(v) -> Optional[int]:
    if v is None:
        return None
    try:
        y = int(v)
    except (TypeError, ValueError):
        return None
    if 1000 <= y <= 2100:
        return y
    return None


def coerce_country(v) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip().upper()
    if s in ("?", "NULL", "NONE", ""):
        return None
    if len(s) == 2:
        return s
    # Map a few common phrasings the model may emit.
    aliases = {
        "ENGLAND": "GB", "UK": "GB", "BRITAIN": "GB", "GREAT BRITAIN": "GB",
        "FRANCE": "FR", "GERMANY": "DE", "ITALY": "IT", "SPAIN": "ES",
        "NETHERLANDS": "NL", "USA": "US", "UNITED STATES": "US",
    }
    return aliases.get(s)


def insert_match(source: Source, match: dict, cost: float) -> bool:
    excerpt = (match.get("excerpt") or "").strip()
    if not excerpt or len(excerpt) < 80:
        return False
    if len(excerpt) > 1500:
        excerpt = excerpt[:1500]

    confidence = match.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    location = match.get("location_text")
    if isinstance(location, str):
        location = location.strip() or None
    elif location is not None:
        location = str(location)

    row = {
        "text_source": source.text_source,
        "text_source_url": source.text_source_url,
        "text_publication_year": source.publication_year,
        "event_year_min": coerce_year(match.get("event_year_min")),
        "event_year_max": coerce_year(match.get("event_year_max")),
        "country": coerce_country(match.get("country")),
        "location_text": location,
        "excerpt": excerpt,
        "extracted_summary": (match.get("summary") or "").strip() or None,
        "confidence": confidence,
        "model": MODEL,
        "cost_usd": round(cost, 6),
    }

    try:
        db.table("historical_records").upsert(
            row,
            on_conflict="text_source,excerpt_hash",
            ignore_duplicates=False,
        ).execute()
        return True
    except Exception as e:
        print(f"  INSERT FAIL: {e}", file=sys.stderr)
        return False


# ===========================================================================
# Driver
# ===========================================================================
def process_source(source: Source, anth: Anthropic, http: httpx.Client, budget: dict) -> dict:
    stats = {"slug": source.slug, "chunks": 0, "calls": 0, "matches": 0, "inserted": 0, "cost": 0.0}

    print(f"\n=== {source.text_source} [{source.slug}] ===")
    raw = fetch_text(source, http)
    if not raw:
        print("  (no text fetched, skipping)")
        return stats

    text = apply_window(raw, source.window)
    if len(text) > INPUT_CAP_CHARS:
        text = text[:INPUT_CAP_CHARS]
    print(f"  text length: {len(text):,} chars (raw {len(raw):,})")

    chunks = chunk_text(text)
    chunks = [c for c in chunks if len(c) >= MIN_CHUNK_CHARS]
    stats["chunks"] = len(chunks)

    if source.prefilter:
        kept = [
            c for c in chunks
            if any(kw in c.lower() for kw in PREFILTER_KEYWORDS)
        ]
        stats["prefilter_kept"] = len(kept)
        print(f"  chunks: {len(chunks)} (after prefilter: {len(kept)})")
        chunks = kept
    else:
        print(f"  chunks: {len(chunks)}")

    for i, chunk in enumerate(chunks):
        if budget["calls"] >= MAX_LLM_CALLS:
            print(f"  hit MAX_LLM_CALLS={MAX_LLM_CALLS}, stopping")
            break
        if budget["cost"] >= COST_ABORT_USD:
            print(f"  hit COST_ABORT_USD=${COST_ABORT_USD}, stopping")
            break

        try:
            matches, cost = call_claude(anth, source, chunk)
        except Exception as e:
            print(f"  LLM error on chunk {i}: {e}", file=sys.stderr)
            time.sleep(1.0)
            continue

        budget["calls"] += 1
        budget["cost"] += cost
        stats["calls"] += 1
        stats["cost"] += cost

        for m in matches:
            stats["matches"] += 1
            if insert_match(source, m, cost):
                stats["inserted"] += 1

        if (budget["calls"] % 10) == 0:
            print(
                f"  [running] calls={budget['calls']} cost=${budget['cost']:.4f} "
                f"({source.slug} chunk {i+1}/{len(chunks)})"
            )

    print(
        f"  done {source.slug}: chunks={stats['chunks']} calls={stats['calls']} "
        f"matches={stats['matches']} inserted={stats['inserted']} cost=${stats['cost']:.4f}"
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        help="Only run a single source slug (e.g. mowing-devil-1678).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + chunk only; skip Claude and insert.",
    )
    args = parser.parse_args()

    if not (Path(ROOT / ".env").exists()):
        print("ERROR: .env not found at project root", file=sys.stderr)
        return 1

    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_KEY"):
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY not set", file=sys.stderr)
        return 1

    sources = SOURCES
    if args.source:
        sources = [s for s in SOURCES if s.slug == args.source]
        if not sources:
            print(f"ERROR: unknown source slug '{args.source}'", file=sys.stderr)
            print(f"Known slugs: {', '.join(s.slug for s in SOURCES)}", file=sys.stderr)
            return 1

    anth = Anthropic()
    budget = {"calls": 0, "cost": 0.0}
    all_stats: list[dict] = []

    with httpx.Client(headers={"User-Agent": UA}) as http:
        for source in sources:
            if args.dry_run:
                raw = fetch_text(source, http)
                if not raw:
                    print(f"[dry-run] {source.slug}: fetch failed")
                    continue
                text = apply_window(raw, source.window)
                if len(text) > INPUT_CAP_CHARS:
                    text = text[:INPUT_CAP_CHARS]
                chunks = [c for c in chunk_text(text) if len(c) >= MIN_CHUNK_CHARS]
                kept = chunks
                if source.prefilter:
                    kept = [
                        c for c in chunks
                        if any(kw in c.lower() for kw in PREFILTER_KEYWORDS)
                    ]
                print(
                    f"[dry-run] {source.slug}: raw={len(raw):,} windowed={len(text):,} "
                    f"chunks={len(chunks)} prefilter_kept={len(kept)}"
                )
                continue
            stats = process_source(source, anth, http, budget)
            all_stats.append(stats)
            if budget["calls"] >= MAX_LLM_CALLS or budget["cost"] >= COST_ABORT_USD:
                break

    print("\n=== SUMMARY ===")
    total_in = total_calls = total_match = 0
    for s in all_stats:
        print(
            f"  {s['slug']:24s} chunks={s['chunks']:3d} calls={s['calls']:3d} "
            f"matches={s['matches']:2d} inserted={s['inserted']:2d} "
            f"cost=${s['cost']:.4f}"
        )
        total_in += s["inserted"]
        total_calls += s["calls"]
        total_match += s["matches"]
    print(
        f"  TOTAL                    calls={total_calls} matches={total_match} "
        f"inserted={total_in} cost=${budget['cost']:.4f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
