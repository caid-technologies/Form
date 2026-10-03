-- Disposable Postgres database only; psql -v ON_ERROR_STOP=1 -f this-file.sql.
begin;
create role anon;
create role authenticated;
create role service_role;
\ir ../../supabase/migrations/20260908000200_opencode_bridge.sql
\ir ../../supabase/migrations/20260930152646_opencode_stale_sessions.sql
-- Reapplying the migration must be safe.
\ir ../../supabase/migrations/20260930152646_opencode_stale_sessions.sql
create function pg_temp.assert_true(ok boolean, label text) returns void language plpgsql as $$
begin if ok is distinct from true then raise exception 'Assertion failed: %', label; end if; end;
$$;
insert into public.opencode_sessions values
  ('s', 'mini', 'owner', '00000000-0000-0000-0000-000000000001', 'active', 'nonce', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z', null, 1),
  ('idle', 'mini', 'owner', '00000000-0000-0000-0000-000000000002', 'active', 'nonce', '2026-09-30T11:00:00Z', '2026-09-30T11:00:00Z', null, 1);
insert into public.opencode_commands (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, created_at, updated_at)
  values ('c', 's', 'mini', 'owner', '00000000-0000-0000-0000-000000000001', 'project_message', 'one', 'queued', 'digest', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z');
select public.reconcile_opencode_session('s', '2026-09-30T12:01:59Z');
select pg_temp.assert_true((select count(*) = 0 from public.opencode_events), 'no early warning');
select public.reconcile_opencode_session('s', '2026-09-30T12:02:00Z');
select public.reconcile_opencode_session('s', '2026-09-30T12:04:59Z');
select pg_temp.assert_true((select count(*) = 1 from public.opencode_events), 'one warning during grace');
select pg_temp.assert_true((select status = 'active' from public.opencode_sessions where session_id = 's'), 'active during grace');
-- Heartbeats allow a request to run longer than five minutes.
update public.opencode_sessions set last_heartbeat_at = '2026-09-30T12:04:59Z' where session_id = 's';
select public.reconcile_opencode_session('s', '2026-09-30T12:05:00Z');
select pg_temp.assert_true((select status = 'active' from public.opencode_sessions where session_id = 's'), 'reconnect resumes');
select public.reconcile_opencode_session('s', '2026-09-30T12:09:59Z');
select public.reconcile_opencode_session('s', '2026-09-30T12:10:00Z');
select pg_temp.assert_true((select status = 'failed' from public.opencode_sessions where session_id = 's'), 'terminal timeout');
select pg_temp.assert_true((select status = 'failed' and lease_token_hash is null and lease_expires_at is null from public.opencode_commands where command_id = 'c'), 'lease revoked');
select pg_temp.assert_true((select count(*) = 1 from public.opencode_events where event_id = 'c:terminal' and event_json->'error'->>'code' = 'connector_timeout'), 'one canonical timeout');
select public.reconcile_opencode_session('idle', '2026-09-30T12:10:00Z');
select pg_temp.assert_true((select status = 'active' from public.opencode_sessions where session_id = 'idle'), 'idle sessions do not time out');
insert into public.opencode_commands (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, created_at, updated_at)
  values ('new', 'idle', 'mini', 'owner', '00000000-0000-0000-0000-000000000002', 'project_message', 'new', 'queued', 'digest', '2026-09-30T12:10:00Z', '2026-09-30T12:10:00Z');
select public.reconcile_opencode_session('idle', '2026-09-30T12:10:00Z');
select pg_temp.assert_true((select status = 'active' from public.opencode_sessions where session_id = 'idle'), 'fresh command gets full grace');
select public.reconcile_opencode_session('idle', '2026-09-30T12:12:00Z', true);
select public.reconcile_opencode_session('idle', '2026-09-30T12:20:00Z');
select pg_temp.assert_true((select status = 'cancelled' from public.opencode_commands where command_id = 'new'), 'offline cancellation');
select pg_temp.assert_true((select count(*) = 0 from public.opencode_events where session_id = 'idle' and event_json->>'kind' = 'failed'), 'cancel stays cancelled');
do $$ begin
  begin
    update public.opencode_commands set status = 'running' where command_id = 'c';
    raise exception 'revived a failed command';
  exception when check_violation then null; end;
  begin
    insert into public.opencode_commands (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, created_at, updated_at)
      values ('late', 's', 'mini', 'owner', 'project', 'project_message', 'late', 'queued', 'digest', '2026-09-30T12:20:00Z', '2026-09-30T12:20:00Z');
    raise exception 'inserted work in failed session';
  exception when check_violation then null; end;
end $$;
select pg_temp.assert_true(not has_function_privilege('anon', 'public.reconcile_opencode_session(text,timestamptz,boolean)', 'execute'), 'anon cannot settle sessions');
select pg_temp.assert_true(not has_function_privilege('authenticated', 'public.reconcile_opencode_session(text,timestamptz,boolean)', 'execute'), 'authenticated cannot settle sessions');
select pg_temp.assert_true(has_function_privilege('service_role', 'public.reconcile_opencode_session(text,timestamptz,boolean)', 'execute'), 'backend can settle sessions');
grant select, update, insert on public.opencode_sessions, public.opencode_commands, public.opencode_events to service_role;
set role service_role;
select public.reconcile_opencode_session('s', '2026-09-30T12:30:00Z');
reset role;
rollback;
