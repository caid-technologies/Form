-- Serialize claims per session across gateway processes. The row lock also
-- coordinates with cancellation, timeout reconciliation, and command insertion.
create index if not exists idx_opencode_commands_session_pending
  on public.opencode_commands (connector_id, session_id, created_at, command_id)
  where status in ('queued', 'leased', 'running');

create or replace function public.claim_opencode_command(
  p_connector_id text, p_session_id text, p_lease_hash text,
  p_lease_seconds integer default 60, p_now timestamptz default now()
) returns setof public.opencode_commands
language plpgsql security invoker set search_path = '' as $$
declare
  v_id text;
  v_now text := to_char(p_now at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
  v_expiry text := to_char((p_now + make_interval(secs => greatest(15, least(p_lease_seconds, 300))))
    at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
begin
  if p_lease_hash is null or p_lease_hash = '' then
    raise invalid_parameter_value using message = 'A command lease hash is required';
  end if;
  perform session_id from public.opencode_sessions
    where session_id = p_session_id and connector_id = p_connector_id and status = 'active' for update;
  if not found then return; end if;
  -- Do not skip locked commands: that could bypass an active/renewing command.
  -- Session then command matches the existing reconciliation lock order.
  perform command_id from public.opencode_commands
    where connector_id = p_connector_id and session_id = p_session_id
      and status in ('queued', 'leased', 'running') order by created_at, command_id for update;
  if exists (select 1 from public.opencode_commands
      where connector_id = p_connector_id and session_id = p_session_id
      and status in ('leased', 'running') and lease_expires_at::timestamptz > p_now) then
    return;
  end if;
  select command_id into v_id from public.opencode_commands
    where connector_id = p_connector_id and session_id = p_session_id
      and (status = 'queued' or (status in ('leased', 'running') and lease_expires_at::timestamptz <= p_now))
    order by created_at, command_id limit 1;
  if v_id is null then return; end if;
  return query update public.opencode_commands set status = 'leased', attempt_count = attempt_count + 1,
      lease_expires_at = v_expiry, lease_token_hash = p_lease_hash, updated_at = v_now
    where command_id = v_id returning *;
end;
$$;
revoke all on function public.claim_opencode_command(text, text, text, integer, timestamptz) from public, anon, authenticated;
grant execute on function public.claim_opencode_command(text, text, text, integer, timestamptz) to service_role;
