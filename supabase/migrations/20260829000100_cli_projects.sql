-- Private project manifests and immutable revision ancestry for forma-oss.

create table if not exists public.cli_projects (
  project_id text primary key,
  workspace_id text,
  owner_user_id text not null,
  title text not null default '',
  creation_channel text not null default 'cli',
  visibility text not null default 'public',
  current_revision integer not null default 0 check (current_revision >= 0),
  current_revision_id text,
  created_at text not null,
  updated_at text not null
);

create index if not exists idx_cli_projects_owner_updated
  on public.cli_projects (owner_user_id, updated_at desc);

create table if not exists public.cli_project_revisions (
  revision_id text primary key,
  project_id text not null references public.cli_projects(project_id) on delete cascade,
  owner_user_id text not null,
  revision integer not null check (revision >= 1),
  parent_revision_id text,
  manifest_json jsonb not null,
  created_at text not null,
  constraint cli_project_revisions_project_revision_unique unique (project_id, revision)
);

create index if not exists idx_cli_project_revisions_owner_project
  on public.cli_project_revisions (owner_user_id, project_id, revision desc);

create table if not exists public.cli_device_authorizations (
  device_code_hash text primary key,
  user_code_hash text not null unique,
  status text not null default 'pending',
  expires_at double precision not null,
  owner_user_id text,
  provider text,
  email text,
  display_name text,
  consumed boolean not null default false,
  created_at text not null
);

create index if not exists idx_cli_device_authorizations_user_code
  on public.cli_device_authorizations (user_code_hash);

create table if not exists public.cli_token_sessions (
  token_hash text primary key,
  token_type text not null,
  refresh_token_hash text,
  owner_user_id text not null,
  provider text not null,
  email text,
  display_name text,
  expires_at double precision not null,
  revoked_at double precision,
  created_at text not null
);

create index if not exists idx_cli_token_sessions_refresh_hash
  on public.cli_token_sessions (refresh_token_hash);

alter table public.cli_projects enable row level security;
alter table public.cli_project_revisions enable row level security;
alter table public.cli_device_authorizations enable row level security;
alter table public.cli_token_sessions enable row level security;

revoke all on table public.cli_projects from anon, authenticated;
revoke all on table public.cli_project_revisions from anon, authenticated;
revoke all on table public.cli_device_authorizations from anon, authenticated;
revoke all on table public.cli_token_sessions from anon, authenticated;
grant select, insert, update, delete on table public.cli_projects to service_role;
grant select, insert, update, delete on table public.cli_project_revisions to service_role;
grant select, insert, update, delete on table public.cli_device_authorizations to service_role;
grant select, insert, update, delete on table public.cli_token_sessions to service_role;

comment on table public.cli_projects is
  'Private project identities created explicitly by the forma-oss CLI.';
comment on table public.cli_project_revisions is
  'Immutable, secret-free project manifests uploaded by the forma-oss CLI.';
comment on table public.cli_device_authorizations is
  'Short-lived hashed device authorization sessions for the forma-oss CLI.';
comment on table public.cli_token_sessions is
  'Hashed forma-oss CLI access and refresh token sessions with revocation state.';
