-- ============================================================================
-- Migration: 0005_lat_lng_columns.sql
-- Purpose:   Expose location as JSON-friendly lat/lng numbers via PostgREST.
--            geography columns serialize as opaque WKB hex; clients want
--            decimal degrees. Generated columns keep them in lockstep with
--            location.
-- ============================================================================

alter table crop_circles.formations
  add column lat numeric
    generated always as (st_y(location::geometry)) stored,
  add column lng numeric
    generated always as (st_x(location::geometry)) stored;

create index formations_latlng_idx on crop_circles.formations (lat, lng)
  where lat is not null and lng is not null;
