-- Revocable links to immutable project revisions. Raw tokens never enter storage.
create table if not exists public.project_shares (
  id text primary key,
  project_id text not null references public.projects(project_id) on delete cascade,
  revision_id text not null references public.project_revisions(id) on delete cascade,
  owner_user_id text not null,
  token_hash varchar(64) not null unique,
  created_at text not null,
  revoked_at text
);

create index if not exists ix_project_shares_owner_revision_created
  on public.project_shares (project_id, owner_user_id, revision_id, created_at desc, id desc);
create index if not exists ix_project_shares_revision_id
  on public.project_shares (revision_id);

-- Clerk/CLI ownership and capability checks are performed by the backend.
-- No client role may list hashes or create/revoke links through the Data API.
alter table public.project_shares enable row level security;
revoke all on table public.project_shares from public, anon, authenticated;
grant select, insert, update, delete on table public.project_shares to service_role;

comment on table public.project_shares is
  'Owner-issued, revision-scoped share token hashes with individual revocation.';
