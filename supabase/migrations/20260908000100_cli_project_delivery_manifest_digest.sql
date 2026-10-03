-- Persist the canonical payload digest without rewriting the already-applied
-- CLI delivery table migration.

alter table public.cli_project_deliveries
  add column if not exists manifest_digest text not null default '';
