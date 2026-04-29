-- ============================================================================
-- Migration: 0003_grants.sql
-- Purpose:   Grant Supabase API roles access to the crop_circles schema.
--            Required because PostgREST connects via the authenticator role,
--            which switches to anon/authenticated/service_role per request.
-- ============================================================================

-- service_role bypasses RLS and is what our pipeline uses.
-- anon/authenticated get USAGE only; per-table SELECT will come with RLS later.

grant usage on schema crop_circles to anon, authenticated, service_role;

grant all on all tables    in schema crop_circles to service_role;
grant all on all sequences in schema crop_circles to service_role;
grant all on all functions in schema crop_circles to service_role;

alter default privileges in schema crop_circles
    grant all on tables to service_role;
alter default privileges in schema crop_circles
    grant all on sequences to service_role;
alter default privileges in schema crop_circles
    grant all on functions to service_role;
