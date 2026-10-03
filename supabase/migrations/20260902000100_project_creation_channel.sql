-- Give every project identity an explicit creation channel.

create table if not exists public.projects (
  project_id text primary key,
  owner_user_id text,
  creation_channel text not null,
  title text not null default '',
  prompt text not null default '',
  chat_id text,
  workspace_id text,
  visibility text not null default 'private',
  status text not null default 'active',
  created_at text not null,
  updated_at text not null
);

create index if not exists idx_projects_owner_updated
  on public.projects (owner_user_id, updated_at desc);

alter table public.projects enable row level security;
revoke all on table public.projects from anon, authenticated;
grant select, insert, update, delete on table public.projects to service_role;

insert into public.projects (
  project_id, owner_user_id, creation_channel, title, prompt, chat_id,
  visibility, status, created_at, updated_at
)
select project_id, owner_user_id, 'hosted', title, prompt, chat_id,
       visibility, status, created_at, created_at
from public.generated_projects
on conflict (project_id) do nothing;

insert into public.projects (
  project_id, owner_user_id, creation_channel, title, prompt, workspace_id,
  visibility, status, created_at, updated_at
)
select p.project_id, p.owner_user_id, 'cli', p.title,
       coalesce(r.manifest_json->>'prompt', ''), p.workspace_id,
       'private', 'active', p.created_at, p.updated_at
from public.cli_projects p
left join public.cli_project_revisions r on r.revision_id = p.current_revision_id
on conflict (project_id) do nothing;

alter table public.generated_projects
  add column if not exists creation_channel text not null default 'hosted';

alter table public.cli_projects
  add column if not exists creation_channel text not null default 'cli';

alter table public.generated_projects
  drop constraint if exists generated_projects_creation_channel_check;

alter table public.generated_projects
  add constraint generated_projects_creation_channel_check
  check (creation_channel in ('hosted', 'cli'));

alter table public.cli_projects
  drop constraint if exists cli_projects_creation_channel_check;

alter table public.cli_projects
  add constraint cli_projects_creation_channel_check
  check (creation_channel = 'cli');

create index if not exists idx_generated_projects_creation_channel
  on public.generated_projects (creation_channel);

comment on column public.generated_projects.creation_channel is
  'Product surface through which the project entered Forma.';

comment on column public.cli_projects.creation_channel is
  'Product surface through which the project entered Forma.';
