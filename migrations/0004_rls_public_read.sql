-- ============================================================================
-- Migration: 0004_rls_public_read.sql
-- Purpose:   Enable RLS on every table; allow public read on catalog + canonical
--            metadata; lock raw scrapes and LLM cost ledgers to service_role.
-- ============================================================================

-- Public-readable: catalog + canonical metadata
alter table crop_circles.sources             enable row level security;
alter table crop_circles.formations          enable row level security;
alter table crop_circles.formation_aliases   enable row level security;
alter table crop_circles.formation_images    enable row level security;
alter table crop_circles.formation_encodings enable row level security;

-- Locked: raw scrapes, LLM cost ledgers, per-record extractions
alter table crop_circles.source_records         enable row level security;
alter table crop_circles.extraction_runs        enable row level security;
alter table crop_circles.formation_extractions  enable row level security;

-- Public-read policies
create policy cc_public_read_sources on crop_circles.sources
    for select to anon, authenticated using (true);

create policy cc_public_read_formations on crop_circles.formations
    for select to anon, authenticated using (true);

create policy cc_public_read_aliases on crop_circles.formation_aliases
    for select to anon, authenticated using (true);

-- Only redistributable images leak to anon callers
create policy cc_public_read_images on crop_circles.formation_images
    for select to anon, authenticated using (can_redistribute = true);

create policy cc_public_read_encodings on crop_circles.formation_encodings
    for select to anon, authenticated using (true);

-- Without these grants the policies never run; PostgREST hits a 403 first.
grant select on crop_circles.sources             to anon, authenticated;
grant select on crop_circles.formations          to anon, authenticated;
grant select on crop_circles.formation_aliases   to anon, authenticated;
grant select on crop_circles.formation_images    to anon, authenticated;
grant select on crop_circles.formation_encodings to anon, authenticated;
