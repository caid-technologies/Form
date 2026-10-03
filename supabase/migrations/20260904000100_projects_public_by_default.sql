-- Project visibility is a project concern and is public by default.
alter table if exists public.projects
  alter column visibility set default 'public';

alter table if exists public.generated_projects
  alter column visibility set default 'public';

update public.projects
set visibility = 'public'
where visibility is distinct from 'public';

update public.generated_projects
set visibility = 'public'
where visibility is distinct from 'public';
