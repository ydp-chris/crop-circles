"""
db.py — Supabase PostgREST client for the crop_circles schema.

Uses supabase-py with the service_role key. Direct Postgres (psycopg) was
considered first but the rest of the YDP stack is service_role + PostgREST,
so we follow that pattern for credential consistency across agents.

Setup:
    pip install -r requirements.txt
    cp .env.example .env       # then fill in SUPABASE_URL + SUPABASE_SERVICE_KEY
    python db.py               # smoke test — should print sources + counts

Note: The crop_circles schema must be added to "Exposed schemas" in
Supabase Dashboard → Project Settings → API before this will work.
"""
from __future__ import annotations

import os
from typing import Any, Optional
from uuid import UUID

import structlog
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
log = structlog.get_logger()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set. See .env.example."
    )

_client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
db = _client.schema("crop_circles")


# ============================================================================
# sources
# ============================================================================
def get_source_id(slug: str) -> UUID:
    res = db.table("sources").select("id").eq("slug", slug).limit(1).execute()
    if not res.data:
        raise LookupError(f"Source not registered: {slug}")
    return UUID(res.data[0]["id"])


# ============================================================================
# source_records — raw scraped HTML/text, the immutable layer
# ============================================================================
def upsert_source_record(
    source_slug: str,
    source_record_id: str,
    source_url: str,
    *,
    raw_html: Optional[str] = None,
    raw_text: Optional[str] = None,
    parsed_json: Optional[dict[str, Any]] = None,
    http_status: Optional[int] = None,
) -> UUID:
    """Idempotent on (source_id, source_record_id)."""
    payload = {
        "source_id": str(get_source_id(source_slug)),
        "source_record_id": source_record_id,
        "source_url": source_url,
        "raw_html": raw_html,
        "raw_text": raw_text,
        "parsed_json": parsed_json,
        "http_status": http_status,
    }
    res = (
        db.table("source_records")
        .upsert(payload, on_conflict="source_id,source_record_id")
        .execute()
    )
    return UUID(res.data[0]["id"])


def already_scraped(source_slug: str, source_record_id: str) -> bool:
    """Cheap skip-check for resumable scrapers."""
    source_id = str(get_source_id(source_slug))
    res = (
        db.table("source_records")
        .select("id")
        .eq("source_id", source_id)
        .eq("source_record_id", source_record_id)
        .limit(1)
        .execute()
    )
    return bool(res.data)


# ============================================================================
# formations
# ============================================================================
def get_formation_by_canonical_id(canonical_id: str) -> Optional[UUID]:
    res = (
        db.table("formations")
        .select("id")
        .eq("canonical_id", canonical_id)
        .limit(1)
        .execute()
    )
    return UUID(res.data[0]["id"]) if res.data else None


def insert_formation(
    canonical_id: str,
    *,
    event_date=None,
    country: Optional[str] = None,
    county: Optional[str] = None,
    nearest_landmark: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    crop_type: Optional[str] = None,
    diameter_m: Optional[float] = None,
    n_components: Optional[int] = None,
    notes: Optional[str] = None,
) -> UUID:
    """Insert via RPC — wraps the PostGIS ST_MakePoint call server-side."""
    params = {
        "p_canonical_id": canonical_id,
        "p_event_date": event_date.isoformat() if event_date else None,
        "p_country": country,
        "p_county": county,
        "p_nearest_landmark": nearest_landmark,
        "p_lat": lat,
        "p_lng": lng,
        "p_crop_type": crop_type,
        "p_diameter_m": diameter_m,
        "p_n_components": n_components,
        "p_notes": notes,
    }
    res = db.rpc("cc_insert_formation", params).execute()
    return UUID(res.data)


def link_alias(
    formation_id: UUID,
    source_slug: str,
    source_record_id: str,
    source_url: Optional[str] = None,
    is_primary: bool = False,
) -> None:
    """Idempotent link between a canonical formation and an archive's record."""
    payload = {
        "formation_id": str(formation_id),
        "source_id": str(get_source_id(source_slug)),
        "source_record_id": source_record_id,
        "source_url": source_url,
        "is_primary": is_primary,
    }
    (
        db.table("formation_aliases")
        .upsert(
            payload,
            on_conflict="source_id,source_record_id",
            ignore_duplicates=True,
        )
        .execute()
    )


# ============================================================================
# Smoke test
# ============================================================================
if __name__ == "__main__":
    sources = (
        db.table("sources").select("slug,name").order("slug").execute().data
    )
    n_form = (
        db.table("formations")
        .select("id", count="exact", head=True)
        .execute()
        .count
    )
    n_rec = (
        db.table("source_records")
        .select("id", count="exact", head=True)
        .execute()
        .count
    )

    print("Connected to crop_circles schema via PostgREST.")
    print(f"  Sources:        {len(sources)}")
    print(f"  Formations:     {n_form}")
    print(f"  Source records: {n_rec}")
    print()
    for s in sources:
        print(f"  - {s['slug']:20s} {s['name']}")
