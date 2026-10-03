-- Keep the canonical identity's current revision pointer in sync with CLI pushes.

alter table public.projects
  add column if not exists current_revision integer not null default 0,
  add column if not exists current_revision_id text;
