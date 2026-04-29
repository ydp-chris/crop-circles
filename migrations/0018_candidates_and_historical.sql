-- ============================================================================
-- Migration: 0018_candidates_and_historical.sql
-- Purpose:   Two new tables for active discovery work:
--              candidate_formations - live news/reddit candidates being tracked
--              historical_records   - pre-photographic textual references
-- ============================================================================

create table crop_circles.candidate_formations (
    id                    uuid primary key default gen_random_uuid(),
    source_kind           text not null check (source_kind in
        ('news','reddit','twitter','blog','rss','other')),
    source_url            text not null,
    source_title          text,
    mentioned_date        date,
    mentioned_year        integer,
    mentioned_location    text,
    country               text,
    lat                   numeric,
    lng                   numeric,
    raw_text              text,
    confidence            numeric(4,3),
    is_in_corpus          boolean not null default false,
    matched_formation_id  uuid references crop_circles.formations(id) on delete set null,
    status                text not null default 'new'
        check (status in ('new','reviewed','promoted','rejected','duplicate')),
    notes                 text,
    discovered_at         timestamptz not null default now(),
    unique (source_url)
);

create index candidate_formations_status_idx
    on crop_circles.candidate_formations (status, discovered_at desc);
create index candidate_formations_country_idx
    on crop_circles.candidate_formations (country);

create table crop_circles.historical_records (
    id                    uuid primary key default gen_random_uuid(),
    text_source           text not null,
    text_source_url       text,
    text_publication_year integer,
    event_year_min        integer,
    event_year_max        integer,
    country               text,
    location_text         text,
    lat                   numeric,
    lng                   numeric,
    excerpt               text not null,
    excerpt_hash          text generated always as (md5(excerpt)) stored,
    extracted_summary     text,
    confidence            numeric(4,3),
    notes                 text,
    model                 text,
    cost_usd              numeric(10,6),
    created_at            timestamptz not null default now(),
    unique (text_source, excerpt_hash)
);

create index historical_records_year_idx
    on crop_circles.historical_records (event_year_min);

alter table crop_circles.candidate_formations enable row level security;
alter table crop_circles.historical_records   enable row level security;

create policy cc_public_read_candidates on crop_circles.candidate_formations
    for select to anon, authenticated using (status in ('new','reviewed','promoted'));

create policy cc_public_read_historical on crop_circles.historical_records
    for select to anon, authenticated using (true);

grant select on crop_circles.candidate_formations to anon, authenticated;
grant select on crop_circles.historical_records   to anon, authenticated;
grant all on crop_circles.candidate_formations to service_role;
grant all on crop_circles.historical_records   to service_role;
