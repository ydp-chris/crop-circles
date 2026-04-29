-- ============================================================================
-- Migration: 0001_init_crop_circles.sql
-- Project:   ValleySomm Supabase (sodzbrrgcvtznosboznq)
-- Purpose:   Initialize crop_circles schema for corpus research.
-- Scope:     MVP tables only (CV/biology/witness tables deferred).
-- ============================================================================

-- Extensions (no-op if already enabled)
create extension if not exists postgis;
create extension if not exists vector;
create extension if not exists pgcrypto;

-- Namespace
create schema if not exists crop_circles;

-- ============================================================================
-- sources: registry of archives we ingest from
-- ============================================================================
create table crop_circles.sources (
    id                       uuid primary key default gen_random_uuid(),
    slug                     text not null unique,
    name                     text not null,
    base_url                 text,
    license_summary          text,
    can_redistribute_images  boolean not null default false,
    scrape_strategy          text,
    notes                    text,
    last_full_scrape_at      timestamptz,
    created_at               timestamptz not null default now(),
    updated_at               timestamptz not null default now()
);

comment on table crop_circles.sources is
    'Registry of crop circle archives. License + redistribution flags drive what we can publish later.';

-- ============================================================================
-- formations: canonical record, one row per real-world event
-- ============================================================================
create table crop_circles.formations (
    id                        uuid primary key default gen_random_uuid(),
    canonical_id              text unique,                          -- Müller-style, e.g. UK20260404_A
    event_date                date,
    event_date_uncertainty    text,                                 -- 'early July 1991', 'circa 1980'
    country                   text,                                 -- ISO-3166-1 alpha-2
    county                    text,
    nearest_landmark          text,                                 -- 'Avebury', 'Stonehenge'
    location                  geography(point, 4326),
    location_precision_m      integer,
    crop_type                 text,                                 -- wheat, barley, rapeseed, oats, maize, linseed, grass
    diameter_m                numeric(10, 2),
    n_components              integer,
    symmetry_order            integer,                              -- D_n group, computed later
    status                    text not null default 'documented'
        check (status in ('documented', 'claimed_human', 'verified_human', 'disputed', 'merged_duplicate')),
    notes                     text,
    first_documented_at       timestamptz,
    created_at                timestamptz not null default now(),
    updated_at                timestamptz not null default now()
);

create index formations_location_gist  on crop_circles.formations using gist (location);
create index formations_event_date_idx on crop_circles.formations (event_date);
create index formations_country_idx    on crop_circles.formations (country);
create index formations_status_idx     on crop_circles.formations (status);

comment on table crop_circles.formations is
    'One row per real-world formation event. Multiple archives may document the same event; merge via formation_aliases.';

-- ============================================================================
-- formation_aliases: many archives → one canonical formation
-- ============================================================================
create table crop_circles.formation_aliases (
    id                uuid primary key default gen_random_uuid(),
    formation_id      uuid not null references crop_circles.formations(id) on delete cascade,
    source_id         uuid not null references crop_circles.sources(id)    on delete restrict,
    source_record_id  text not null,
    source_url        text,
    is_primary        boolean not null default false,
    created_at        timestamptz not null default now(),
    unique (source_id, source_record_id)
);

create index formation_aliases_formation_idx on crop_circles.formation_aliases (formation_id);

comment on table crop_circles.formation_aliases is
    'Cross-archive identity. Each archive''s ID for the same event maps to one canonical formation.';

-- ============================================================================
-- source_records: raw scraped content (immutable source of truth)
-- ============================================================================
create table crop_circles.source_records (
    id                uuid primary key default gen_random_uuid(),
    source_id         uuid not null references crop_circles.sources(id) on delete restrict,
    source_record_id  text not null,
    source_url        text,
    formation_id      uuid references crop_circles.formations(id) on delete set null,
    raw_html          text,
    raw_text          text,
    parsed_json       jsonb,
    http_status       integer,
    scraped_at        timestamptz not null default now(),
    unique (source_id, source_record_id)
);

create index source_records_formation_idx   on crop_circles.source_records (formation_id);
create index source_records_scraped_at_idx  on crop_circles.source_records (scraped_at);

comment on table crop_circles.source_records is
    'Raw scraped HTML/text per source, before LLM extraction. Never overwritten — re-extraction reads from here.';

-- ============================================================================
-- formation_images: many images per formation
-- ============================================================================
create table crop_circles.formation_images (
    id                uuid primary key default gen_random_uuid(),
    formation_id      uuid not null references crop_circles.formations(id) on delete cascade,
    source_id         uuid references crop_circles.sources(id) on delete set null,
    source_url        text,
    photographer      text,
    photo_date        date,
    image_kind        text check (image_kind in
        ('aerial', 'oblique', 'ground', 'drone', 'satellite', 'diagram', 'vector', 'unknown')),
    local_path        text,                       -- path on the Pi filesystem
    width             integer,
    height            integer,
    content_hash      text,                       -- sha256 hex
    phash             bigint,                     -- 64-bit perceptual hash
    dhash             bigint,
    license           text,
    license_notes     text,
    can_redistribute  boolean not null default false,
    created_at        timestamptz not null default now()
);

create index formation_images_formation_idx     on crop_circles.formation_images (formation_id);
create index formation_images_phash_idx         on crop_circles.formation_images (phash);
create index formation_images_content_hash_idx  on crop_circles.formation_images (content_hash);

comment on table crop_circles.formation_images is
    'One row per image. phash powers cross-archive dedup; local_path points to bytes on Pi filesystem.';

-- ============================================================================
-- extraction_runs: audit trail of LLM passes
-- ============================================================================
create table crop_circles.extraction_runs (
    id                  uuid primary key default gen_random_uuid(),
    model               text not null,            -- 'claude-opus-4-7' etc.
    prompt_version      text not null,
    started_at          timestamptz not null default now(),
    finished_at         timestamptz,
    total_records       integer,
    successful_records  integer,
    failed_records      integer,
    cost_usd            numeric(10, 4),
    notes               text
);

comment on table crop_circles.extraction_runs is
    'Audit trail for LLM-based structured extraction. Re-run with newer models without losing history.';

-- ============================================================================
-- formation_extractions: per-record LLM output
-- ============================================================================
create table crop_circles.formation_extractions (
    id                  uuid primary key default gen_random_uuid(),
    source_record_id    uuid not null references crop_circles.source_records(id) on delete cascade,
    extraction_run_id   uuid not null references crop_circles.extraction_runs(id) on delete cascade,
    extracted_json      jsonb not null,
    confidence          numeric(4, 3),
    conflicts           jsonb,                    -- where this disagrees with prior data
    created_at          timestamptz not null default now()
);

create index formation_extractions_source_record_idx on crop_circles.formation_extractions (source_record_id);
create index formation_extractions_run_idx           on crop_circles.formation_extractions (extraction_run_id);

-- ============================================================================
-- formation_encodings: decoded messages (Pi, Euler, Arecibo, etc.)
-- ============================================================================
create table crop_circles.formation_encodings (
    id                    uuid primary key default gen_random_uuid(),
    formation_id          uuid not null references crop_circles.formations(id) on delete cascade,
    encoding_type         text not null check (encoding_type in (
        'binary_ascii', 'pi', 'eulers_identity', 'arecibo_response',
        'planetary_alignment', 'fractal', 'sacred_geometry', 'unknown', 'other'
    )),
    decoded_text          text,
    decoder_name          text,                   -- 'Vigay', 'Reed', 'Treurniet'
    decoder_method        text,
    decoder_confidence    numeric(4, 3),
    community_acceptance  text check (community_acceptance in
        ('verified', 'plausible', 'contested', 'fringe', 'disputed')),
    source_citation       text,
    notes                 text,
    created_at            timestamptz not null default now()
);

create index formation_encodings_formation_idx on crop_circles.formation_encodings (formation_id);
create index formation_encodings_type_idx      on crop_circles.formation_encodings (encoding_type);

comment on table crop_circles.formation_encodings is
    'Decoded messages claimed in formations. encoding_candidates table (deferred) will hold un-decoded suspects.';

-- ============================================================================
-- updated_at trigger
-- ============================================================================
create or replace function crop_circles.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger sources_set_updated_at
    before update on crop_circles.sources
    for each row execute function crop_circles.set_updated_at();

create trigger formations_set_updated_at
    before update on crop_circles.formations
    for each row execute function crop_circles.set_updated_at();

-- ============================================================================
-- Seed sources
-- ============================================================================
insert into crop_circles.sources
    (slug, name, base_url, license_summary, can_redistribute_images, scrape_strategy)
values
    ('cropcirclecenter',  'Crop Circle Center',
        'https://www.cropcirclecenter.com/',
        'All rights reserved', false, 'deterministic_url_scheme'),
    ('cra',               'Circle Research Archive',
        'https://circleresearcharchive.com/',
        'Mixed; image rights with original holders', false, 'wp_rest_api'),
    ('pringle',           'Lucy Pringle Archive',
        'http://www.lucypringle.co.uk/',
        'All rights reserved; research use OK', false, 'predictable_paths'),
    ('mueller',           'International Crop Circle Archive (Andreas Müller)',
        'https://www.kornkreise-forschung.de/',
        'All rights reserved', false, 'partial_html_scrape'),
    ('connector',         'Crop Circle Connector',
        'https://www.cropcircleconnector.com/',
        'Strict; written permission required', false, 'manual_only'),
    ('temporary_temples', 'Temporary Temples (Steve & Karen Alexander)',
        'https://temporarytemples.co.uk/',
        'View only; reproduction restricted', false, 'wp_rest_api'),
    ('blt',               'BLT Research Team',
        'https://www.bltresearch.com/',
        'Research use', false, 'tabular_pdf'),
    ('shapesofwisdom',    'Shapes of Wisdom',
        'https://shapesofwisdom.com/',
        'Paid; not redistributable', false, 'thumbnails_only'),
    ('wikimedia',         'Wikimedia Commons',
        'https://commons.wikimedia.org/wiki/Category:Crop_circles',
        'CC-BY-SA / CC0', true, 'mediawiki_api'),
    ('flickr_cc',         'Flickr (CC-licensed)',
        'https://www.flickr.com/',
        'CC variants', true, 'flickr_api');

-- ============================================================================
-- End migration 0001
-- ============================================================================
