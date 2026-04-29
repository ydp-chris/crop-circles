-- ============================================================================
-- Migration: 0015_wave_day_narratives.sql
-- Purpose:   Claude-generated narrative paragraphs for the top cross-country
--            wave days. Read by /findings page; written by a one-off script.
-- ============================================================================

create table crop_circles.wave_day_narratives (
    id              uuid primary key default gen_random_uuid(),
    wave_date       date not null unique,
    n_countries     integer not null,
    n_formations    integer not null,
    countries       text[],
    narrative       text not null,
    model           text not null,
    cost_usd        numeric(10, 6),
    created_at      timestamptz not null default now()
);

create index wave_day_narratives_date_idx on crop_circles.wave_day_narratives (wave_date desc);

alter table crop_circles.wave_day_narratives enable row level security;

create policy cc_public_read_wave_narratives on crop_circles.wave_day_narratives
    for select to anon, authenticated using (true);

grant select on crop_circles.wave_day_narratives to anon, authenticated;
grant all on crop_circles.wave_day_narratives to service_role;
