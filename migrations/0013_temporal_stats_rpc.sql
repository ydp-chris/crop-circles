-- ============================================================================
-- Migration: 0013_temporal_stats_rpc.sql
-- Purpose:   Single RPC returning lunar / monthly / weekday / yearly stats for
--            the public /findings page. Includes chi-square against uniform
--            for the lunar test.
-- ============================================================================

create or replace function crop_circles.cc_temporal_stats()
returns jsonb
language plpgsql
security invoker
stable
set statement_timeout to '15s'
as $$
declare
    v_total           integer;
    v_lunar_chi_sq    numeric;
    v_lunar_data      jsonb;
    v_monthly_data    jsonb;
    v_weekday_data    jsonb;
    v_yearly_data     jsonb;
begin
    select count(*) into v_total
      from crop_circles.formations
     where event_date is not null;

    with phase_counts as (
        select moon_phase_name as phase,
               count(*) as observed,
               (v_total::numeric / 8.0) as expected
          from crop_circles.formations
         where event_date is not null
           and moon_phase_name is not null
         group by moon_phase_name
    )
    select
        round(sum(power(observed - expected, 2) / expected), 2),
        jsonb_agg(jsonb_build_object(
            'phase', phase,
            'count', observed,
            'expected', round(expected, 1),
            'ratio', round(observed / expected, 3)
        ) order by case phase
            when 'new' then 0 when 'waxing_crescent' then 1
            when 'first_quarter' then 2 when 'waxing_gibbous' then 3
            when 'full' then 4 when 'waning_gibbous' then 5
            when 'last_quarter' then 6 when 'waning_crescent' then 7
        end)
    into v_lunar_chi_sq, v_lunar_data
    from phase_counts;

    with month_counts as (
        select extract(month from event_date)::int as mo,
               count(*) as n
          from crop_circles.formations
         where event_date is not null
         group by mo
         order by mo
    )
    select jsonb_agg(jsonb_build_object(
        'month', mo,
        'count', n,
        'pct', round(100.0 * n / v_total, 1)
    ) order by mo) into v_monthly_data
    from month_counts;

    with dow_counts as (
        select extract(dow from event_date)::int as d,
               count(*) as n
          from crop_circles.formations
         where event_date is not null
         group by d
    )
    select jsonb_agg(jsonb_build_object(
        'dow', d,
        'name', case d
            when 0 then 'Sun' when 1 then 'Mon' when 2 then 'Tue'
            when 3 then 'Wed' when 4 then 'Thu' when 5 then 'Fri'
            when 6 then 'Sat'
        end,
        'count', n,
        'pct', round(100.0 * n / v_total, 1)
    ) order by d) into v_weekday_data
    from dow_counts;

    with year_counts as (
        select extract(year from event_date)::int as yr,
               count(*) as total,
               count(*) filter (where country = 'GB') as uk,
               count(*) filter (where country is not null and country != 'GB') as other
          from crop_circles.formations
         where event_date is not null
         group by yr
    )
    select jsonb_agg(jsonb_build_object(
        'year', yr,
        'count', total,
        'uk', uk,
        'other', other
    ) order by yr) into v_yearly_data
    from year_counts;

    return jsonb_build_object(
        'total_with_date', v_total,
        'lunar', jsonb_build_object(
            'chi_square', v_lunar_chi_sq,
            'df', 7,
            'critical_005', 14.07,
            'critical_001', 18.48,
            'is_significant_005', v_lunar_chi_sq > 14.07,
            'by_phase', v_lunar_data
        ),
        'monthly', v_monthly_data,
        'weekday', v_weekday_data,
        'yearly', v_yearly_data
    );
end;
$$;

grant execute on function crop_circles.cc_temporal_stats() to anon, authenticated, service_role;
