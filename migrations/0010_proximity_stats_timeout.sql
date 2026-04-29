-- ============================================================================
-- Migration: 0010_proximity_stats_timeout.sql
-- Purpose:   anon role has statement_timeout=3s but cc_proximity_stats runs
--            ~10s (5495 sites × 500 random points distance computation).
--            Override per-function so public-facing /findings page works.
-- ============================================================================

create or replace function crop_circles.cc_proximity_stats(
    p_random_n integer default 500
)
returns table (
    formation_count            integer,
    random_count               integer,
    formation_median_m         integer,
    random_median_m            integer,
    formation_mean_m           integer,
    random_mean_m              integer,
    formation_within_500m_pct  numeric,
    random_within_500m_pct     numeric,
    formation_within_1km_pct   numeric,
    random_within_1km_pct      numeric,
    formation_within_2km_pct   numeric,
    random_within_2km_pct      numeric
)
language plpgsql
security invoker
stable
set statement_timeout to '15s'
as $$
begin
    perform setseed(0.42);

    return query
    with formation_dist as (
        select f.id,
               (select min(st_distance(f.location, s.location))::int
                  from crop_circles.heritage_sites s
                 where st_dwithin(f.location, s.location, 50000)) as nearest_m
          from crop_circles.formations f
         where f.location is not null
           and (f.location_precision_m is null or f.location_precision_m <= 1000)
    ),
    random_pts as (
        select gs as id,
               st_setsrid(st_makepoint(
                 -3.5 + random() * 4.0,
                 50.5 + random() * 1.5
               ), 4326)::geography as location
          from generate_series(1, p_random_n) gs
    ),
    random_dist as (
        select r.id,
               (select min(st_distance(r.location, s.location))::int
                  from crop_circles.heritage_sites s
                 where st_dwithin(r.location, s.location, 50000)) as nearest_m
          from random_pts r
    ),
    f_stats as (
        select count(*) as n,
               percentile_disc(0.5) within group (order by nearest_m) as med,
               round(avg(nearest_m)) as mean,
               count(*) filter (where nearest_m <= 500)  as w500,
               count(*) filter (where nearest_m <= 1000) as w1k,
               count(*) filter (where nearest_m <= 2000) as w2k
          from formation_dist
         where nearest_m is not null
    ),
    r_stats as (
        select count(*) as n,
               percentile_disc(0.5) within group (order by nearest_m) as med,
               round(avg(nearest_m)) as mean,
               count(*) filter (where nearest_m <= 500)  as w500,
               count(*) filter (where nearest_m <= 1000) as w1k,
               count(*) filter (where nearest_m <= 2000) as w2k
          from random_dist
         where nearest_m is not null
    )
    select
        f.n::integer,
        r.n::integer,
        f.med::integer,
        r.med::integer,
        f.mean::integer,
        r.mean::integer,
        round(100.0 * f.w500 / nullif(f.n, 0), 1),
        round(100.0 * r.w500 / nullif(r.n, 0), 1),
        round(100.0 * f.w1k / nullif(f.n, 0), 1),
        round(100.0 * r.w1k / nullif(r.n, 0), 1),
        round(100.0 * f.w2k / nullif(f.n, 0), 1),
        round(100.0 * r.w2k / nullif(r.n, 0), 1)
    from f_stats f cross join r_stats r;
end;
$$;
