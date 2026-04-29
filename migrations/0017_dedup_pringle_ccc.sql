-- ============================================================================
-- Migration: 0017_dedup_pringle_ccc.sql
-- Purpose:   Cross-archive dedup. Pringle and CCC use incompatible canonical_id
--            formats (lowercase 2-letter sequence vs uppercase YYYYMMDD-letter)
--            so canonical_id matching never fires. This RPC matches by
--            (event_date, country) with trigram similarity tiebreaker on
--            landmark + county text.
--
-- Strategies, in order:
--   1. Unambiguous: exactly one CCC formation on the same (event_date, country)
--   2. Ambiguous (2-4 CCC formations same date/country): merge if best
--      trigram similarity >= 0.4
-- Skipped: 5+ CCC matches (too noisy) or no CCC match (keep Pringle as primary)
--
-- After running, formation_aliases / formation_images / source_records all
-- repoint at the surviving CCC formation; the Pringle row is deleted; the
-- CCC row's NULL fields are filled from Pringle.
-- ============================================================================

create extension if not exists pg_trgm;

create or replace function crop_circles.cc_dedup_pringle_ccc()
returns table (
    merged_unambiguous integer,
    merged_similarity  integer,
    skipped_ambiguous  integer,
    pringle_kept       integer
)
language plpgsql
security invoker
set statement_timeout to '60s'
as $$
declare
    v_pringle_src    uuid;
    v_ccc_src        uuid;
    v_unambig        integer := 0;
    v_sim_merged     integer := 0;
    v_skipped        integer := 0;
    v_kept           integer := 0;
begin
    select id into v_pringle_src from crop_circles.sources where slug = 'pringle';
    select id into v_ccc_src     from crop_circles.sources where slug = 'cropcirclecenter';

    create temp table _candidates on commit drop as
    with pringle as (
        select distinct f.id as pid, f.event_date, f.country,
               f.county, f.nearest_landmark
          from crop_circles.formations f
          join crop_circles.formation_aliases fa on fa.formation_id = f.id
         where fa.source_id = v_pringle_src
           and f.event_date is not null
           and f.country is not null
           and not exists (
             select 1 from crop_circles.formation_aliases fa2
              where fa2.formation_id = f.id and fa2.source_id = v_ccc_src
           )
    ),
    ccc as (
        select f.id as cid, f.event_date, f.country,
               f.county, f.nearest_landmark
          from crop_circles.formations f
          join crop_circles.formation_aliases fa on fa.formation_id = f.id
         where fa.source_id = v_ccc_src
           and f.event_date is not null
    )
    select
        p.pid,
        c.cid,
        greatest(
            similarity(coalesce(p.nearest_landmark, ''), coalesce(c.nearest_landmark, '')),
            similarity(coalesce(p.county, ''), coalesce(c.county, ''))
        ) as sim,
        count(*) over (partition by p.pid)            as ccc_count,
        row_number() over (
            partition by p.pid
            order by greatest(
                similarity(coalesce(p.nearest_landmark, ''), coalesce(c.nearest_landmark, '')),
                similarity(coalesce(p.county, ''), coalesce(c.county, ''))
            ) desc
        ) as rank_within
    from pringle p
    join ccc c on c.event_date = p.event_date and c.country = p.country;

    create temp table _merges on commit drop as
    select pid, cid, sim,
           case when ccc_count = 1 then 'unambiguous'
                when ccc_count between 2 and 4 and sim >= 0.4 then 'similarity'
                else 'skip' end as strategy
      from _candidates
     where rank_within = 1;

    select count(*) filter (where strategy = 'unambiguous'),
           count(*) filter (where strategy = 'similarity'),
           count(*) filter (where strategy = 'skip')
      into v_unambig, v_sim_merged, v_skipped
      from _merges;

    update crop_circles.formation_aliases fa
       set formation_id = m.cid
      from _merges m
     where fa.formation_id = m.pid
       and m.strategy in ('unambiguous', 'similarity');

    update crop_circles.formation_images fi
       set formation_id = m.cid
      from _merges m
     where fi.formation_id = m.pid
       and m.strategy in ('unambiguous', 'similarity');

    update crop_circles.source_records sr
       set formation_id = m.cid
      from _merges m
     where sr.formation_id = m.pid
       and m.strategy in ('unambiguous', 'similarity');

    update crop_circles.formations c
       set nearest_landmark = coalesce(c.nearest_landmark, p.nearest_landmark),
           county           = coalesce(c.county, p.county),
           crop_type        = coalesce(c.crop_type, p.crop_type),
           diameter_m       = coalesce(c.diameter_m, p.diameter_m),
           notes            = coalesce(c.notes, p.notes)
      from crop_circles.formations p
      join _merges m on m.pid = p.id
     where c.id = m.cid
       and m.strategy in ('unambiguous', 'similarity');

    delete from crop_circles.formations f
     using _merges m
     where f.id = m.pid
       and m.strategy in ('unambiguous', 'similarity');

    select count(distinct f.id)
      into v_kept
      from crop_circles.formations f
      join crop_circles.formation_aliases fa on fa.formation_id = f.id
     where fa.source_id = v_pringle_src;

    return query select v_unambig, v_sim_merged, v_skipped, v_kept;
end;
$$;

grant execute on function crop_circles.cc_dedup_pringle_ccc() to service_role;
