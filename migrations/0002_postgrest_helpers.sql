-- ============================================================================
-- Migration: 0002_postgrest_helpers.sql
-- Purpose:   RPC wrappers for operations PostgREST can't express directly,
--            primarily PostGIS geography construction during inserts.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- cc_insert_formation: insert a formation, building location from lat/lng
-- ----------------------------------------------------------------------------
create or replace function crop_circles.cc_insert_formation(
    p_canonical_id     text,
    p_event_date       date     default null,
    p_country          text     default null,
    p_county           text     default null,
    p_nearest_landmark text     default null,
    p_lat              numeric  default null,
    p_lng              numeric  default null,
    p_crop_type        text     default null,
    p_diameter_m       numeric  default null,
    p_n_components     integer  default null,
    p_notes            text     default null
)
returns uuid
language plpgsql
security invoker
as $$
declare
    v_id  uuid;
    v_loc geography;
begin
    if p_lat is not null and p_lng is not null then
        v_loc := st_setsrid(st_makepoint(p_lng, p_lat), 4326)::geography;
    end if;

    insert into crop_circles.formations
        (canonical_id, event_date, country, county, nearest_landmark,
         location, crop_type, diameter_m, n_components, notes)
    values
        (p_canonical_id, p_event_date, p_country, p_county, p_nearest_landmark,
         v_loc, p_crop_type, p_diameter_m, p_n_components, p_notes)
    returning id into v_id;

    return v_id;
end;
$$;

grant execute on function crop_circles.cc_insert_formation(
    text, date, text, text, text, numeric, numeric, text, numeric, integer, text
) to service_role;

-- ----------------------------------------------------------------------------
-- cc_set_formation_location: update location on an existing formation
-- ----------------------------------------------------------------------------
create or replace function crop_circles.cc_set_formation_location(
    p_formation_id uuid,
    p_lat          numeric,
    p_lng          numeric
)
returns void
language sql
security invoker
as $$
    update crop_circles.formations
       set location = st_setsrid(st_makepoint(p_lng, p_lat), 4326)::geography
     where id = p_formation_id;
$$;

grant execute on function crop_circles.cc_set_formation_location(uuid, numeric, numeric)
    to service_role;

-- ============================================================================
-- End migration 0002
-- ============================================================================
