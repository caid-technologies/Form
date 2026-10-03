-- Canonical project visibility publish audit trail.
-- Mirrors project_deletion_audit: content-free lifecycle records written by service_role.
-- The application registers this table in init_db; without it the hosted backend
-- fails startup (PGRST205) and every /api endpoint returns 500.

create table if not exists public.project_publish_audit (
  id text primary key,
  project_id text not null,
  owner_user_id text not null,
  acting_user_id text not null,
  visibility_before text not null,
  created_at text not null
);

create index if not exists idx_project_publish_audit_project_created
  on public.project_publish_audit (project_id, created_at desc);

create index if not exists idx_project_publish_audit_owner_created
  on public.project_publish_audit (owner_user_id, created_at desc);

create index if not exists idx_project_publish_audit_actor_created
  on public.project_publish_audit (acting_user_id, created_at desc);

alter table public.project_publish_audit enable row level security;

revoke all on table public.project_publish_audit from anon, authenticated;

grant select, insert, update, delete on table public.project_publish_audit to service_role;

comment on table public.project_publish_audit is
  'Project visibility publish events without project content.';