-- Keep CLI project projections aligned with the canonical project visibility.
alter table if exists public.cli_projects
  add column if not exists visibility text not null default 'public';

update public.cli_projects
set visibility = 'public'
where visibility is distinct from 'public';

alter table if exists public.cli_projects
  drop constraint if exists cli_projects_visibility_check;

alter table if exists public.cli_projects
  add constraint cli_projects_visibility_check
  check (visibility in ('public', 'private'));
