-- ============================================================================
-- Migration: 0016_more_stats_rpc.sql
-- Purpose:   Three more analytical slices for /findings:
--              1. Day-of-month aggregate distribution
--              2. Sunday share by year (smartphone-era weakening?)
--              3. Source canon - formations documented in multiple archives
-- ============================================================================

create or replace function crop_circles.cc_more_stats()
returns jsonb
language plpgsql
security invoker
stable
set statement_timeout to '15s'
as $$
declare
    v_dom              jsonb;
    v_sunday_by_year   jsonb;
    v_canon            jsonb;
begin
    with dom_counts as (
        select extract(day from event_date)::int as dom,
               count(*) as n
          from crop_circles.formations
         where event_date is not null
         group by dom
    )
    select jsonb_agg(jsonb_build_object('day', dom, 'count', n) order by dom)
      into v_dom
      from dom_counts;

    with year_dow as (
        select extract(year from event_date)::int as yr,
               count(*) as total,
               count(*) filter (where extract(dow from event_date) = 0) as sundays
          from crop_circles.formations
         where event_date is not null
         group by yr
    )
    select jsonb_agg(jsonb_build_object(
        'year', yr,
        'total', total,
        'sundays', sundays,
        'sunday_pct',
            case when total > 0 then round(100.0 * sundays / total, 1)
                 else null end
    ) order by yr) into v_sunday_by_year
    from year_dow
    where total >= 10;

    with formation_alias_counts as (
        select formation_id,
               count(*) as n_aliases,
               array_agg(distinct source_id) as source_ids
          from crop_circles.formation_aliases
         group by formation_id
        having count(distinct source_id) >= 2
    )
    select jsonb_agg(jsonb_build_object(
        'canonical_id', f.canonical_id,
        'event_date', f.event_date,
        'country', f.country,
        'county', f.county,
        'nearest_landmark', f.nearest_landmark,
        'n_aliases', fac.n_aliases,
        'source_slugs',
            (select array_agg(s.slug order by s.slug)
               from crop_circles.sources s
              where s.id = any(fac.source_ids))
    ) order by fac.n_aliases desc, f.event_date desc nulls last)
    into v_canon
    from formation_alias_counts fac
    join crop_circles.formations f on f.id = fac.formation_id
    limit 30;

    return jsonb_build_object(
        'day_of_month',     v_dom,
        'sunday_by_year',   v_sunday_by_year,
        'canon',            v_canon
    );
end;
$$;

grant execute on function crop_circles.cc_more_stats() to anon, authenticated, service_role;
