-- Disposable Postgres database only; all fixture changes are rolled back.
begin;
create role anon;
create role authenticated;
create role service_role;
\ir ../../supabase/migrations/20260908000200_opencode_bridge.sql
\ir ../../supabase/migrations/20260930152646_opencode_stale_sessions.sql
\ir ../../supabase/migrations/20260930163207_opencode_serial_claims.sql
\ir ../../supabase/migrations/20260930163207_opencode_serial_claims.sql
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
select pg_temp.assert_true((select count(*) = 0 from public.claim_opencode_command('wrong', 's', 'hash', 60, '2026-09-30T12:00:00Z')), 'connector identity required');
select pg_temp.assert_true((select command_id = 'c0' and attempt_count = 1 from public.claim_opencode_command('mini', 's', 'first-hash', 15, '2026-09-30T12:00:00Z')), 'FIFO and first attempt');
select pg_temp.assert_true((select count(*) = 0 from public.claim_opencode_command('mini', 's', 'blocked-hash', 60, '2026-09-30T12:00:14.999999Z')), 'live lease blocks queue');
select pg_temp.assert_true((select command_id = 'other-c' from public.claim_opencode_command('mini', 'other', 'other-hash', 60, '2026-09-30T12:00:00Z')), 'other session progresses');
select pg_temp.assert_true((select command_id = 'c0' and attempt_count = 2 and lease_token_hash = 'new-hash' from public.claim_opencode_command('mini', 's', 'new-hash', 60, '2026-09-30T12:00:15Z')), 'exact expiry reclaims same command');
update public.opencode_commands set status = 'running' where command_id = 'c0';
select pg_temp.assert_true((select count(*) = 0 from public.claim_opencode_command('mini', 's', 'blocked-hash', 60, '2026-09-30T12:00:16Z')), 'running lease blocks queue');
update public.opencode_commands set status = 'succeeded', lease_expires_at = null, lease_token_hash = null where command_id = 'c0';
select pg_temp.assert_true((select command_id = 'c1' from public.claim_opencode_command('mini', 's', 'second-hash', 60, '2026-09-30T12:00:16Z')), 'completion releases next FIFO command');
update public.opencode_commands set status = 'cancelled', lease_expires_at = null, lease_token_hash = null where command_id = 'c1';
select pg_temp.assert_true((select command_id = 'c2' from public.claim_opencode_command('mini', 's', 'third-hash', 60, '2026-09-30T12:00:17Z')), 'cancellation releases next FIFO command');
update public.opencode_sessions set status = 'cancelled' where session_id = 's';
select pg_temp.assert_true((select count(*) = 0 from public.claim_opencode_command('mini', 's', 'late-hash', 60, '2026-09-30T12:05:00Z')), 'closed session cannot reclaim');
select pg_temp.assert_true(not has_function_privilege('anon', 'public.claim_opencode_command(text,text,text,integer,timestamptz)', 'execute'), 'anon cannot claim');
select pg_temp.assert_true(not has_function_privilege('authenticated', 'public.claim_opencode_command(text,text,text,integer,timestamptz)', 'execute'), 'authenticated cannot claim');
select pg_temp.assert_true(has_function_privilege('service_role', 'public.claim_opencode_command(text,text,text,integer,timestamptz)', 'execute'), 'backend can claim');
grant select, update on public.opencode_sessions, public.opencode_commands to service_role;
set role service_role;
select public.claim_opencode_command('mini', 'other', 'service-hash', 60, '2026-09-30T12:01:00Z');
reset role;
rollback;
