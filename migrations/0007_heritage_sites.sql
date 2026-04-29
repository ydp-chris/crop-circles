-- ============================================================================
-- Migration: 0007_heritage_sites.sql
-- Purpose:   Catalog heritage / archaeological / religious sites and link
--            them by proximity to crop circle formations. Quickstart scope:
--            Wessex bbox, sourced from OpenStreetMap (ODbL).
-- ============================================================================

create table crop_circles.heritage_sites (
    id              uuid primary key default gen_random_uuid(),
    osm_type        text not null check (osm_type in ('node','way','relation')),
    osm_id          bigint not null,
    name            text,
    site_type       text not null,
    historic_period text,
    location        geography(point, 4326) not null,
    osm_tags        jsonb,
    source          text not null default 'openstreetmap',
    license         text not null default 'ODbL',
    created_at      timestamptz not null default now(),
    unique (osm_type, osm_id)
);

create index heritage_sites_location_gist on crop_circles.heritage_sites
    using gist (location);
create index heritage_sites_site_type_idx on crop_circles.heritage_sites (site_type);

alter table crop_circles.heritage_sites
  add column lat numeric generated always as (st_y(location::geometry)) stored,
  add column lng numeric generated always as (st_x(location::geometry)) stored;

create table crop_circles.formation_nearby_sites (
    id              uuid primary key default gen_random_uuid(),
    formation_id    uuid not null references crop_circles.formations(id) on delete cascade,
    site_id         uuid not null references crop_circles.heritage_sites(id) on delete cascade,
    distance_m      integer not null,
    bearing_deg     numeric,
    created_at      timestamptz not null default now(),
    unique (formation_id, site_id)
);

create index formation_nearby_sites_formation_idx
    on crop_circles.formation_nearby_sites (formation_id);
create index formation_nearby_sites_distance_idx
    on crop_circles.formation_nearby_sites (distance_m);

alter table crop_circles.heritage_sites          enable row level security;
alter table crop_circles.formation_nearby_sites  enable row level security;

create policy cc_public_read_heritage_sites on crop_circles.heritage_sites
    for select to anon, authenticated using (true);
create policy cc_public_read_nearby on crop_circles.formation_nearby_sites
    for select to anon, authenticated using (true);

grant select on crop_circles.heritage_sites         to anon, authenticated;
grant select on crop_circles.formation_nearby_sites to anon, authenticated;
grant all on crop_circles.heritage_sites            to service_role;
grant all on crop_circles.formation_nearby_sites    to service_role;
