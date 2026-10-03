-- Keep project lifecycle state on the canonical project identity.

alter table public.projects
  add column if not exists deleted_at text,
  add column if not exists deletion_requested_by text,
  add column if not exists purge_after text,
  add column if not exists purge_started_at text,
  add column if not exists purge_completed_at text,
  add column if not exists deletion_error text;

alter table public.projects
  drop constraint if exists projects_status_valid;

alter table public.projects
  add constraint projects_status_valid
  check (status in ('active', 'deletion_pending', 'purging', 'purged', 'deletion_failed'));

create index if not exists idx_projects_purge_after
  on public.projects (purge_after)
  where purge_after is not null;
