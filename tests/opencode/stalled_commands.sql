-- Disposable Postgres database only; all fixture changes are rolled back.
begin;
create role anon;
create role authenticated;
create role service_role;
\ir ../../supabase/migrations/20260908000200_opencode_bridge.sql
\ir ../../supabase/migrations/20260930152646_opencode_stale_sessions.sql
\ir ../../supabase/migrations/20261003142232_opencode_stalled_commands.sql
\ir ../../supabase/migrations/20261003142232_opencode_stalled_commands.sql
create function pg_temp.assert_true(ok boolean, label text) returns void language plpgsql as $$
begin if ok is distinct from true then raise exception 'Assertion failed: %', label; end if; end;
$$;
insert into public.opencode_sessions values
  ('s', 'mini', 'owner', '00000000-0000-0000-0000-000000000001', 'active', 'nonce', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z', null, 1),
  ('other', 'mini', 'owner', '00000000-0000-0000-0000-000000000002', 'active', 'nonce', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z', null, 1);
insert into public.opencode_commands (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, created_at, updated_at)
select 'c' || n, 's', 'mini', 'owner', '00000000-0000-0000-0000-000000000001', 'project_message', n::text, 'queued', 'digest',
       '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z' from generate_series(0, 2) n;
insert into public.opencode_commands (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, created_at, updated_at)
values ('other-c', 'other', 'mini', 'owner', '00000000-0000-0000-0000-000000000002', 'project_message', 'other', 'queued', 'digest', '2026-09-30T12:00:00Z', '2026-09-30T12:00:00Z');
-- Exercise the invoker function as the backend role, including event permissions.
grant select, update on public.opencode_sessions, public.opencode_commands to service_role;
grant select, insert on public.opencode_events to service_role;
set role service_role;
select public.claim_opencode_command('mini', 's', 'hash-1', 15, '2026-09-30T12:00:00Z');
select public.claim_opencode_command('mini', 's', 'hash-2', 15, '2026-09-30T12:00:15Z');
select public.claim_opencode_command('mini', 's', 'hash-3', 15, '2026-09-30T12:00:30Z');
-- Connector polling never resets the per-command attempt budget.
update public.opencode_sessions set last_heartbeat_at = '2026-09-30T12:00:45Z' where session_id = 's';
select public.reconcile_opencode_session('s', '2026-09-30T12:00:45Z');
select public.reconcile_opencode_session('s', '2026-09-30T12:00:45Z');
reset role;
select pg_temp.assert_true((select status = 'failed' and attempt_count = 3 and lease_token_hash is null and completed_at is not null from public.opencode_commands where command_id = 'c0'), 'owner read settles exhausted claim');
select pg_temp.assert_true((select count(*) = 1 from public.opencode_events where event_id = 'c0:terminal' and event_json->'error'->>'code' = 'opencode_command_stalled'), 'one canonical durable failed event');
select pg_temp.assert_true((select next_event_sequence = 2 and status = 'active' from public.opencode_sessions where session_id = 's'), 'session remains available and sequence advances once');
select pg_temp.assert_true((select command_id = 'c1' and attempt_count = 1 from public.claim_opencode_command('mini', 's', 'next', 15, '2026-09-30T12:00:45Z')), 'next queued command can run');
-- A third attempt with a renewed lease must survive beyond the offline cutoff.
update public.opencode_commands set attempt_count = 3, status = 'running', lease_expires_at = '2026-09-30T14:00:00Z' where command_id = 'c1';
update public.opencode_sessions set last_heartbeat_at = '2026-09-30T13:00:00Z' where session_id = 's';
select public.reconcile_opencode_session('s', '2026-09-30T13:00:00Z');
select pg_temp.assert_true((select count(*) = 0 from public.claim_opencode_command('mini', 's', 'blocked', 15, '2026-09-30T13:00:00Z')), 'healthy long-running lease blocks later commands');
select pg_temp.assert_true((select status = 'running' from public.opencode_commands where command_id = 'c1'), 'long-running command remains healthy');
select pg_temp.assert_true((select command_id = 'c2' from public.claim_opencode_command('mini', 's', 'following', 15, '2026-09-30T14:00:00Z')), 'claim path settles exhausted work and preserves FIFO');
select pg_temp.assert_true((select sequence = 2 from public.opencode_events where event_id = 'c1:terminal'), 'terminal sequences remain ordered');
select public.claim_opencode_command('mini', 'other', 'cancel', 15, '2026-09-30T12:00:00Z');
update public.opencode_commands set attempt_count = 3 where command_id = 'other-c';
select public.reconcile_opencode_session('other', '2026-09-30T12:00:15Z', true);
select pg_temp.assert_true((select status = 'cancelled' from public.opencode_commands where command_id = 'other-c'), 'explicit cancellation wins');
select pg_temp.assert_true(not has_function_privilege('anon', 'public.fail_stalled_opencode_commands(text,timestamptz)', 'execute'), 'anonymous cannot settle commands');
select pg_temp.assert_true(not has_function_privilege('authenticated', 'public.fail_stalled_opencode_commands(text,timestamptz)', 'execute'), 'authenticated cannot settle commands');
rollback;
