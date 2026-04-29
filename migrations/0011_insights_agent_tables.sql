-- ============================================================================
-- Migration: 0011_insights_agent_tables.sql
-- Purpose:   Per-day per-metric snapshots and the nightly Claude-generated
--            brief, for the daily insights agent.
-- ============================================================================

create table crop_circles.metric_snapshots (
    id              uuid primary key default gen_random_uuid(),
    snapshot_date   date not null,
    metric_key      text not null,
    value           numeric,
    baseline        numeric,
    z_score         numeric,
    is_anomaly      boolean not null default false,
    details         jsonb,
    created_at      timestamptz not null default now(),
    unique (snapshot_date, metric_key)
);

create index metric_snapshots_date_idx on crop_circles.metric_snapshots (snapshot_date desc);
create index metric_snapshots_key_idx  on crop_circles.metric_snapshots (metric_key, snapshot_date desc);

create table crop_circles.daily_briefs (
    id              uuid primary key default gen_random_uuid(),
    brief_date      date not null unique,
    summary         text,
    bullets         jsonb,
    metric_keys     text[],
    model           text,
    cost_usd        numeric(10, 6),
    created_at      timestamptz not null default now()
);

create index daily_briefs_date_idx on crop_circles.daily_briefs (brief_date desc);

alter table crop_circles.metric_snapshots enable row level security;
alter table crop_circles.daily_briefs     enable row level security;

create policy cc_public_read_metric_snapshots on crop_circles.metric_snapshots
    for select to anon, authenticated using (true);

create policy cc_public_read_daily_briefs on crop_circles.daily_briefs
    for select to anon, authenticated using (true);

grant select on crop_circles.metric_snapshots to anon, authenticated;
grant select on crop_circles.daily_briefs     to anon, authenticated;
grant all on crop_circles.metric_snapshots to service_role;
grant all on crop_circles.daily_briefs     to service_role;
