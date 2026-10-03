-- Idempotent CLI/agent delivery records linking delivered revisions to receipts.
-- Mirrors the cli_projects/cli_project_revisions access model for the
-- agent-delivery flow introduced by the canonical CLI push delivery session.

create table if not exists public.cli_project_deliveries (
  delivery_id text primary key,
  project_id text not null,
  owner_user_id text not null,
  idempotency_key text not null,
  revision_id text not null,
  revision integer not null check (revision >= 0),
  parent_revision_id text,
  manifest_json jsonb not null,
  status text not null default 'pending',
  receipt_json jsonb,
  created_at text not null,
  completed_at text,
  constraint uq_cli_project_delivery_owner_project_key unique (owner_user_id, project_id, idempotency_key)
);

create index if not exists idx_cli_project_deliveries_project_id
  on public.cli_project_deliveries (project_id);
create index if not exists idx_cli_project_deliveries_owner_project_created
  on public.cli_project_deliveries (owner_user_id, project_id, created_at desc);
create index if not exists idx_cli_project_deliveries_status
  on public.cli_project_deliveries (status);
create index if not exists idx_cli_project_deliveries_created_at
  on public.cli_project_deliveries (created_at);

alter table public.cli_project_deliveries enable row level security;
revoke all on table public.cli_project_deliveries from anon, authenticated;
grant select, insert, update, delete on table public.cli_project_deliveries to service_role;

comment on table public.cli_project_deliveries is
  'Idempotent CLI/agent delivery records linking delivered revisions to receipts.';
