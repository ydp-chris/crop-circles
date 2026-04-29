-- ============================================================================
-- Migration: 0014_extra_stats_rpc.sql
-- Purpose:   Six additional time-series / cross-cut slices for the public
--            findings page: crop-type evolution, Wiltshire share over time,
--            country first-appearance, multi-formation wave days, crop-type
--            vs heritage proximity, and a calendar heatmap.
-- ============================================================================

create or replace function crop_circles.cc_extra_stats()
returns jsonb
language plpgsql
security invoker
stable
set statement_timeout to '15s'
as $$
declare
    v_crop_evolution    jsonb;
    v_wilts_share       jsonb;
    v_country_first     jsonb;
    v_wave_days         jsonb;
    v_crop_proximity    jsonb;
    v_calendar_heatmap  jsonb;
begin
    with top_crops as (
        select crop_type
          from crop_circles.formations
         where crop_type is not null
         group by crop_type
         order by count(*) desc
         limit 5
    ),
    yearly as (
        select extract(year from event_date)::int as yr,
               coalesce(
                 case when crop_type in (select crop_type from top_crops)
                      then crop_type
                      else 'other' end,
                 'unknown'
               ) as ct,
               count(*) as n
          from crop_circles.formations
         where event_date is not null
         group by yr, ct
    )
    select jsonb_agg(jsonb_build_object('year', yr, 'crop', ct, 'count', n) order by yr, ct)
      into v_crop_evolution
      from yearly;

    with year_uk as (
        select extract(year from event_date)::int as yr,
               count(*) filter (where country = 'GB' and county = 'Wiltshire') as wilts,
               count(*) filter (where country = 'GB' and county is not null) as uk_total
          from crop_circles.formations
         where event_date is not null
         group by yr
    )
    select jsonb_agg(jsonb_build_object(
        'year', yr,
        'wilts', wilts,
        'uk_total', uk_total,
        'pct',
        case when uk_total > 0
             then round(100.0 * wilts / uk_total, 1)
             else null end
    ) order by yr) into v_wilts_share
    from year_uk
    where uk_total > 0;

    with first_dates as (
        select country,
               min(event_date) as first_seen,
               count(*) as total
          from crop_circles.formations
         where event_date is not null and country is not null
         group by country
    )
    select jsonb_agg(jsonb_build_object(
        'country', country,
        'first_seen', first_seen,
        'total', total
    ) order by first_seen) into v_country_first
    from first_dates;

    with day_groups as (
        select event_date,
               count(*) as n,
               array_agg(distinct country order by country)
                 filter (where country is not null) as countries,
               count(distinct country) filter (where country is not null) as n_countries
          from crop_circles.formations
         where event_date is not null
         group by event_date
        having count(distinct country) filter (where country is not null) >= 3
    )
    select jsonb_agg(jsonb_build_object(
        'date', event_date,
        'n', n,
        'n_countries', n_countries,
        'countries', countries
    ) order by n_countries desc, n desc) into v_wave_days
    from day_groups;

    with crop_prox as (
        select coalesce(f.crop_type, 'unknown') as crop_type,
               count(distinct f.id) as f_count,
               count(distinct f.id) filter (
                   where exists (
                       select 1 from crop_circles.formation_nearby_sites n
                        where n.formation_id = f.id and n.distance_m <= 500
                   )
               ) as within_500m
          from crop_circles.formations f
         where f.location is not null
           and (f.location_precision_m is null or f.location_precision_m <= 1000)
         group by 1
        having count(distinct f.id) >= 1
    )
    select jsonb_agg(jsonb_build_object(
        'crop_type', crop_type,
        'count', f_count,
        'within_500m', within_500m,
        'pct',
        case when f_count > 0
             then round(100.0 * within_500m / f_count, 1)
             else 0 end
    ) order by f_count desc) into v_crop_proximity
    from crop_prox;

    with cells as (
        select extract(month from event_date)::int as mo,
               extract(day   from event_date)::int as dom,
               count(*) as n
          from crop_circles.formations
         where event_date is not null
         group by mo, dom
    )
    select jsonb_agg(jsonb_build_object('month', mo, 'day', dom, 'count', n))
      into v_calendar_heatmap
      from cells;

    return jsonb_build_object(
        'crop_evolution',    v_crop_evolution,
        'wilts_share',       v_wilts_share,
        'country_first',     v_country_first,
        'wave_days',         v_wave_days,
        'crop_proximity',    v_crop_proximity,
        'calendar_heatmap',  v_calendar_heatmap
    );
end;
$$;

grant execute on function crop_circles.cc_extra_stats() to anon, authenticated, service_role;
