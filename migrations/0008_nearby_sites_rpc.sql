-- ============================================================================
-- Migration: 0008_nearby_sites_rpc.sql
-- Purpose:   Recompute formation_nearby_sites in batch via PostGIS ST_DWithin.
--            Only operates on formations with exact coords (precision_m
--            null or <= 1000m); approximate (centroid) formations skipped
--            because distance-to-X from a centroid is geometrically meaningless.
-- ============================================================================

create or replace function crop_circles.cc_recompute_nearby_sites(
    p_radius_m integer default 5000,
    p_max_per_formation integer default 5
)
returns integer
language plpgsql
security invoker
as $$
declare
    v_count integer;
begin
    delete from crop_circles.formation_nearby_sites;

    insert into crop_circles.formation_nearby_sites
        (formation_id, site_id, distance_m, bearing_deg)
    select formation_id, site_id, distance_m, bearing_deg
    from (
        select
            f.id as formation_id,
            s.id as site_id,
            (st_distance(f.location, s.location))::integer as distance_m,
            round(degrees(st_azimuth(
                f.location::geometry, s.location::geometry
            ))::numeric, 1) as bearing_deg,
            row_number() over (
                partition by f.id
                order by st_distance(f.location, s.location)
            ) as rn
        from crop_circles.formations f
        join crop_circles.heritage_sites s
            on st_dwithin(f.location, s.location, p_radius_m)
        where f.location is not null
          and (f.location_precision_m is null or f.location_precision_m <= 1000)
    ) ranked
    where rn <= p_max_per_formation;

    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

grant execute on function crop_circles.cc_recompute_nearby_sites(integer, integer)
    to service_role;
