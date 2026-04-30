-- ============================================================================
-- Migration: 0021_dedup_generalized.sql
-- Purpose:   Generalize the cross-archive dedup to any source-pair, replacing
--            the hard-coded cc_dedup_pringle_ccc. Match by (event_date,
--            country) with trigram similarity tiebreaker on landmark/county.
--            Caller picks which "from" source to merge into which "into"
--            source, so we can drive Pringle->CCC, TempTemples->CCC,
--            Wikimedia->CCC, etc.
-- ============================================================================

create or replace function crop_circles.cc_dedup_pair(
    p_from_slug text,
    p_into_slug text
)
returns table (
    merged_unambiguous integer,
    merged_similarity  integer,
    skipped_ambiguous  integer,
    from_kept          integer
)
language plpgsql
security invoker
set statement_timeout to '120s'
as $$
declare
    v_from_src   uuid;
    v_into_src   uuid;
    v_unambig    integer := 0;
    v_sim        integer := 0;
    v_skipped    integer := 0;
    v_kept       integer := 0;
begin
    select id into v_from_src from crop_circles.sources where slug = p_from_slug;
    select id into v_into_src from crop_circles.sources where slug = p_into_slug;
    if v_from_src is null or v_into_src is null then
        raise exception 'Unknown source slug';
    end if;

    create temp table _candidates on commit drop as
    with from_set as (
        select distinct f.id as fid, f.event_date, f.country,
               f.county, f.nearest_landmark
          from crop_circles.formations f
          join crop_circles.formation_aliases fa on fa.formation_id = f.id
         where fa.source_id = v_from_src
           and f.event_date is not null
           and f.country is not null
           and not exists (
             select 1 from crop_circles.formation_aliases fa2
              where fa2.formation_id = f.id and fa2.source_id = v_into_src
           )
    ),
    into_set as (
        select f.id as iid, f.event_date, f.country,
               f.county, f.nearest_landmark
          from crop_circles.formations f
          join crop_circles.formation_aliases fa on fa.formation_id = f.id
         where fa.source_id = v_into_src
           and f.event_date is not null
    )
    select
        f.fid,
        i.iid,
        greatest(
            similarity(coalesce(f.nearest_landmark, ''), coalesce(i.nearest_landmark, '')),
            similarity(coalesce(f.county, ''), coalesce(i.county, ''))
        ) as sim,
        count(*) over (partition by f.fid) as into_count,
        row_number() over (
            partition by f.fid
            order by greatest(
                similarity(coalesce(f.nearest_landmark, ''), coalesce(i.nearest_landmark, '')),
                similarity(coalesce(f.county, ''), coalesce(i.county, ''))
            ) desc
        ) as rank_within
    from from_set f
    join into_set i on i.event_date = f.event_date and i.country = f.country;

    create temp table _merges on commit drop as
    select fid, iid, sim,
           case when into_count = 1 then 'unambiguous'
                when into_count between 2 and 4 and sim >= 0.4 then 'similarity'
                else 'skip' end as strategy
      from _candidates
     where rank_within = 1;

    select count(*) filter (where strategy = 'unambiguous'),
           count(*) filter (where strategy = 'similarity'),
           count(*) filter (where strategy = 'skip')
      into v_unambig, v_sim, v_skipped
      from _merges;

    update crop_circles.formation_aliases fa
       set formation_id = m.iid
      from _merges m
     where fa.formation_id = m.fid
       and m.strategy in ('unambiguous', 'similarity');

    update crop_circles.formation_images fi
       set formation_id = m.iid
      from _merges m
     where fi.formation_id = m.fid
       and m.strategy in ('unambiguous', 'similarity');

    update crop_circles.source_records sr
       set formation_id = m.iid
      from _merges m
     where sr.formation_id = m.fid
       and m.strategy in ('unambiguous', 'similarity');

    update crop_circles.formations c
       set nearest_landmark = coalesce(c.nearest_landmark, p.nearest_landmark),
           county           = coalesce(c.county, p.county),
           crop_type        = coalesce(c.crop_type, p.crop_type),
           diameter_m       = coalesce(c.diameter_m, p.diameter_m),
           notes            = coalesce(c.notes, p.notes)
      from crop_circles.formations p
      join _merges m on m.fid = p.id
     where c.id = m.iid
       and m.strategy in ('unambiguous', 'similarity');

    delete from crop_circles.formations f
     using _merges m
     where f.id = m.fid
       and m.strategy in ('unambiguous', 'similarity');

    select count(distinct f.id)
      into v_kept
      from crop_circles.formations f
      join crop_circles.formation_aliases fa on fa.formation_id = f.id
     where fa.source_id = v_from_src;

    return query select v_unambig, v_sim, v_skipped, v_kept;
end;
$$;

grant execute on function crop_circles.cc_dedup_pair(text, text) to service_role;
