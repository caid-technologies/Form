-- Run against an empty disposable Postgres database with psql -v ON_ERROR_STOP=1.
begin;
create role anon;
create role authenticated;
create role service_role bypassrls;
-- Only the parent keys are needed to exercise the new migration in isolation.
create table public.projects (project_id text primary key);
create table public.project_revisions (id text primary key);
\ir ../../supabase/migrations/20260924144814_project_share_tokens.sql
\ir ../../supabase/migrations/20260924144814_project_share_tokens.sql

do $$
declare client_role text; operation text;
begin
  assert (select relrowsecurity from pg_class where oid = 'public.project_shares'::regclass), 'RLS must be enabled';
  foreach client_role in array array['anon', 'authenticated'] loop
    foreach operation in array array['SELECT', 'INSERT', 'UPDATE', 'DELETE'] loop
      assert not has_table_privilege(client_role, 'public.project_shares', operation), 'Client roles must not access share records';
    end loop;
  end loop;
end $$;

insert into public.projects values ('project-1');
insert into public.project_revisions values ('revision-1');
set local role service_role;
insert into public.project_shares values ('share-1', 'project-1', 'revision-1', 'owner', repeat('a', 64), '2026-09-24T00:00:00Z', null);
update public.project_shares set revoked_at = '2026-09-24T00:01:00Z' where id = 'share-1';
do $$ begin
  assert (select revoked_at is not null from public.project_shares where id = 'share-1'), 'Service role must be able to read and revoke';
end $$;
reset role;
delete from public.project_revisions where id = 'revision-1';
do $$ begin
  assert not exists (select 1 from public.project_shares), 'Deleting a revision must delete share records';
end $$;
insert into public.project_revisions values ('revision-2');
insert into public.project_shares values ('share-2', 'project-1', 'revision-2', 'owner', repeat('b', 64), '2026-09-24T00:00:00Z', null);
delete from public.projects where project_id = 'project-1';
do $$ begin
  assert not exists (select 1 from public.project_shares), 'Deleting a project must delete share records';
end $$;
rollback;
