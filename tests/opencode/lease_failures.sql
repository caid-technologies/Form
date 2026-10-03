-- Disposable Postgres only. All changes are rolled back.
begin;
create role anon;
create role authenticated;
create role service_role;
\ir ../../supabase/migrations/20260908000200_opencode_bridge.sql
\ir ../../supabase/migrations/20260930152646_opencode_stale_sessions.sql
\ir ../../supabase/migrations/20260930160740_opencode_lease_failures.sql
\ir ../../supabase/migrations/20260930160740_opencode_lease_failures.sql
create function pg_temp.assert_true(ok boolean, label text) returns void language plpgsql as $$
begin if ok is distinct from true then raise exception 'Assertion failed: %', label; end if; end;
$$;
insert into public.opencode_sessions values
  ('s', 'mini', 'owner', '00000000-0000-0000-0000-000000000001', 'active', 'nonce', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z', null, 1);
insert into public.opencode_commands (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, lease_token_hash, lease_expires_at, created_at, updated_at)
  values ('c', 's', 'mini', 'owner', '00000000-0000-0000-0000-000000000001', 'project_message', 'one', 'running', 'digest', 'lease-hash', '2026-09-30T12:01:00Z', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z');
select public.fail_expired_opencode_lease('c', 'lease-hash', '2026-09-30T12:00:59Z');
select pg_temp.assert_true((select status = 'running' from public.opencode_commands where command_id = 'c'), 'live lease survives');
select public.fail_expired_opencode_lease('c', 'wrong-hash', '2026-09-30T12:01:00Z');
select pg_temp.assert_true((select count(*) = 0 from public.opencode_events), 'wrong token cannot fail command');
-- Another worker reclaimed the command; a late failure must not cancel it.
update public.opencode_commands set lease_token_hash = 'new-hash', lease_expires_at = '2026-09-30T12:02:00Z' where command_id = 'c';
select public.fail_expired_opencode_lease('c', 'lease-hash', '2026-09-30T12:01:01Z');
select pg_temp.assert_true((select status = 'running' and lease_token_hash = 'new-hash' from public.opencode_commands where command_id = 'c'), 'new claimant survives');
select public.fail_expired_opencode_lease('c', 'new-hash', '2026-09-30T12:02:00Z');
select public.fail_expired_opencode_lease('c', 'new-hash', '2026-09-30T12:02:01Z');
select pg_temp.assert_true((select status = 'failed' and lease_token_hash is null and lease_expires_at is null from public.opencode_commands where command_id = 'c'), 'expired lease is terminal');
select pg_temp.assert_true((select count(*) = 1 from public.opencode_events where event_id = 'c:terminal' and event_json->'error'->>'code' = 'opencode_lease_expired'), 'one sanitized terminal event');
select pg_temp.assert_true((select status = 'active' and next_event_sequence = 2 from public.opencode_sessions where session_id = 's'), 'session accepts next turn with stable cursor');
select pg_temp.assert_true(not has_function_privilege('anon', 'public.fail_expired_opencode_lease(text,text,timestamptz)', 'execute'), 'anon denied');
select pg_temp.assert_true(not has_function_privilege('authenticated', 'public.fail_expired_opencode_lease(text,text,timestamptz)', 'execute'), 'authenticated denied');
select pg_temp.assert_true(has_function_privilege('service_role', 'public.fail_expired_opencode_lease(text,text,timestamptz)', 'execute'), 'backend allowed');
grant select, update, insert on public.opencode_sessions, public.opencode_commands, public.opencode_events to service_role;
set role service_role;
select public.fail_expired_opencode_lease('c', 'new-hash', '2026-09-30T12:03:00Z');
reset role;
rollback;
