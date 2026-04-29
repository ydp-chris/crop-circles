"""
db.py — connection layer for the crop_circles schema.

Uses psycopg3 with a small connection pool. Reads DATABASE_URL from env.
Direct Postgres (not PostgREST) because we'll do bulk inserts at high cadence.

Setup:
    pip install -r requirements.txt
    cp .env.example .env       # then fill in DATABASE_URL
    python db.py               # smoke test — should print sources + counts
"""
from __future__ import annotations

import atexit
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from uuid import UUID

import structlog
from dotenv import load_dotenv
from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

load_dotenv()
log = structlog.get_logger()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL not set. In Supabase: Project Settings → Database → "
        "Connection string. Use the transaction pooler (port 6543)."
    )


def _configure(conn: Connection) -> None:
    """Set search_path on every pooled connection so unqualified names resolve."""
    with conn.cursor() as cur:
        cur.execute("set search_path to crop_circles, public")


_pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=4,
    configure=_configure,
    kwargs={"row_factory": dict_row},
)
atexit.register(_pool.close)


@contextmanager
def get_conn() -> Iterator[Connection]:
    """Pooled connection. Commits on clean exit, rolls back on exception."""
    with _pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ============================================================================
# sources
# ============================================================================
def get_source_id(slug: str) -> UUID:
    with get_conn() as conn:
        row = conn.execute(
            "select id from sources where slug = %s", (slug,)
        ).fetchone()
    if not row:
        raise LookupError(f"Source not registered: {slug}")
    return row["id"]


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
    """Idempotent on (source_id, source_record_id). Updates fields on conflict."""
    source_id = get_source_id(source_slug)
    with get_conn() as conn:
        row = conn.execute(
            """
            insert into source_records
                (source_id, source_record_id, source_url, raw_html, raw_text,
                 parsed_json, http_status)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (source_id, source_record_id) do update set
                source_url  = excluded.source_url,
                raw_html    = excluded.raw_html,
                raw_text    = excluded.raw_text,
                parsed_json = excluded.parsed_json,
                http_status = excluded.http_status,
                scraped_at  = now()
            returning id
            """,
            (
                source_id, source_record_id, source_url, raw_html, raw_text,
                Jsonb(parsed_json) if parsed_json is not None else None,
                http_status,
            ),
        ).fetchone()
    return row["id"]


def already_scraped(source_slug: str, source_record_id: str) -> bool:
    """Cheap skip-check for resumable scrapers."""
    source_id = get_source_id(source_slug)
    with get_conn() as conn:
        row = conn.execute(
            "select 1 from source_records "
            "where source_id = %s and source_record_id = %s limit 1",
            (source_id, source_record_id),
        ).fetchone()
    return row is not None


# ============================================================================
# formations
# ============================================================================
def get_formation_by_canonical_id(canonical_id: str) -> Optional[UUID]:
    with get_conn() as conn:
        row = conn.execute(
            "select id from formations where canonical_id = %s", (canonical_id,)
        ).fetchone()
    return row["id"] if row else None


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
    """Insert a new formation. Caller ensures canonical_id uniqueness."""
    if lat is not None and lng is not None:
        loc_clause = sql.SQL("st_setsrid(st_makepoint(%s, %s), 4326)::geography")
        loc_params: tuple = (lng, lat)  # Postgres ST_MakePoint takes (lng, lat)
    else:
        loc_clause = sql.SQL("null")
        loc_params = ()

    q = sql.SQL("""
        insert into formations
            (canonical_id, event_date, country, county, nearest_landmark,
             location, crop_type, diameter_m, n_components, notes)
        values (%s, %s, %s, %s, %s, {loc}, %s, %s, %s, %s)
        returning id
    """).format(loc=loc_clause)

    params = (
        (canonical_id, event_date, country, county, nearest_landmark)
        + loc_params
        + (crop_type, diameter_m, n_components, notes)
    )

    with get_conn() as conn:
        row = conn.execute(q, params).fetchone()
    return row["id"]


def link_alias(
    formation_id: UUID,
    source_slug: str,
    source_record_id: str,
    source_url: Optional[str] = None,
    is_primary: bool = False,
) -> None:
    """Idempotent link between a canonical formation and an archive's record."""
    source_id = get_source_id(source_slug)
    with get_conn() as conn:
        conn.execute(
            """
            insert into formation_aliases
                (formation_id, source_id, source_record_id, source_url, is_primary)
            values (%s, %s, %s, %s, %s)
            on conflict (source_id, source_record_id) do nothing
            """,
            (formation_id, source_id, source_record_id, source_url, is_primary),
        )


# ============================================================================
# Smoke test
# ============================================================================
if __name__ == "__main__":
    with get_conn() as conn:
        sources = conn.execute(
            "select slug, name from sources order by slug"
        ).fetchall()
        n_form = conn.execute(
            "select count(*) as n from formations"
        ).fetchone()["n"]
        n_rec = conn.execute(
            "select count(*) as n from source_records"
        ).fetchone()["n"]

    print(f"Connected to crop_circles schema.")
    print(f"  Sources:        {len(sources)}")
    print(f"  Formations:     {n_form}")
    print(f"  Source records: {n_rec}")
    print()
    for s in sources:
        print(f"  - {s['slug']:20s} {s['name']}")
