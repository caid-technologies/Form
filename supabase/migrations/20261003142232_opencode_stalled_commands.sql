-- Bound abandoned command claims independently of the connector's poll heartbeat.
-- A live command lease can be renewed indefinitely; only expired attempts count.
create or replace function public.fail_stalled_opencode_commands(
  p_session_id text, p_now timestamptz default now()
) returns void language plpgsql security invoker set search_path = '' as $$
declare
  v_session public.opencode_sessions%rowtype;
  v_command public.opencode_commands%rowtype;
  v_sequence integer;
  v_id text;
  v_now text := to_char(p_now at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
begin
  select * into v_session from public.opencode_sessions where session_id = p_session_id for update;
  if not found or v_session.status <> 'active' then return; end if;
  select greatest(v_session.next_event_sequence, coalesce(max(sequence), 0) + 1)
    into v_sequence from public.opencode_events where session_id = p_session_id;
  for v_command in
    select * from public.opencode_commands where session_id = p_session_id
      and status in ('leased', 'running') and attempt_count >= 3
      and (lease_expires_at is null or lease_expires_at::timestamptz <= p_now)
      order by created_at, command_id for update
  loop
    update public.opencode_commands set status = 'failed', completed_at = v_now, updated_at = v_now,
      lease_token_hash = null, lease_expires_at = null where command_id = v_command.command_id;
    v_id := v_command.command_id || ':terminal';
    insert into public.opencode_events values
      (v_id, p_session_id, v_session.owner_user_id, v_session.project_id, v_sequence,
       jsonb_build_object('event_id', v_id, 'sequence', v_sequence, 'session_id', p_session_id,
         'project_id', v_session.project_id, 'kind', 'failed', 'status', 'failed', 'message', null,
         'revision_id', null, 'validation', null, 'artifact_ids', '[]'::jsonb, 'design_outcome', null,
         'created_at', v_now, 'error', jsonb_build_object('code', 'opencode_command_stalled',
           'message', 'Forma Agent could not start or resume this request after three attempts. Check the runtime and retry the request.',
           'correlation_id', 'event_' || v_id)), v_now)
      on conflict (event_id) do nothing;
    if found then v_sequence := v_sequence + 1; end if;
  end loop;
  update public.opencode_sessions set next_event_sequence = greatest(next_event_sequence, v_sequence)
    where session_id = p_session_id;
end;
$$;
revoke all on function public.fail_stalled_opencode_commands(text, timestamptz) from public, anon, authenticated;
grant execute on function public.fail_stalled_opencode_commands(text, timestamptz) to service_role;

create or replace function public.reconcile_opencode_session(
  p_session_id text, p_now timestamptz default now(), p_cancel boolean default false
) returns void language plpgsql security invoker set search_path = '' as $$
declare
  v_session public.opencode_sessions%rowtype;
  v_command public.opencode_commands%rowtype;
  v_first_created timestamptz;
  v_first_id text;
  v_anchor timestamptz;
  v_age double precision;
  v_status text;
  v_id text;
  v_sequence integer;
  v_now text := to_char(p_now at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
  v_event jsonb;
begin
  select * into v_session from public.opencode_sessions where session_id = p_session_id for update;
  if not found or v_session.status <> 'active' then return; end if;
  if not p_cancel then
    perform public.fail_stalled_opencode_commands(p_session_id, p_now);
    select * into v_session from public.opencode_sessions where session_id = p_session_id;
  end if;
  -- Lock commands and recheck their status before making the timeout decision.
  perform command_id from public.opencode_commands where session_id = p_session_id
    and status in ('queued', 'leased', 'running') order by created_at for update;
  select command_id, created_at::timestamptz into v_first_id, v_first_created
    from public.opencode_commands where session_id = p_session_id
    and status in ('queued', 'leased', 'running') order by created_at limit 1;
  if v_first_id is null and not p_cancel then return; end if;
  v_anchor := greatest(coalesce(v_session.last_heartbeat_at, v_session.created_at)::timestamptz, v_first_created);
  v_age := extract(epoch from (p_now - v_anchor));
  if not p_cancel and v_age < 120 then return; end if;
  select greatest(v_session.next_event_sequence, coalesce(max(sequence), 0) + 1)
    into v_sequence from public.opencode_events where session_id = p_session_id;
  if p_cancel or v_age >= 300 then
    v_status := case when p_cancel then 'cancelled' else 'failed' end;
    update public.opencode_sessions set status = v_status, updated_at = v_now where session_id = p_session_id;
    for v_command in
      update public.opencode_commands set status = v_status, completed_at = v_now, updated_at = v_now,
        lease_token_hash = null, lease_expires_at = null
      where session_id = p_session_id and status in ('queued', 'leased', 'running') returning *
    loop
      v_id := v_command.command_id || ':terminal';
      v_event := jsonb_build_object('event_id', v_id, 'sequence', v_sequence, 'session_id', p_session_id,
        'project_id', v_session.project_id, 'kind', v_status, 'status', v_status, 'message', null,
        'revision_id', null, 'validation', null, 'artifact_ids', '[]'::jsonb, 'design_outcome', null,
        'created_at', v_now, 'error', case when p_cancel then null else jsonb_build_object(
          'code', 'connector_timeout',
          'message', 'Forma Agent did not reconnect within five minutes. Retry this request when it is available.',
          'correlation_id', 'event_' || v_id) end);
      insert into public.opencode_events values
        (v_id, p_session_id, v_session.owner_user_id, v_session.project_id, v_sequence, v_event, v_now)
        on conflict (event_id) do nothing;
      if found then v_sequence := v_sequence + 1; end if;
    end loop;
  end if;
  if p_cancel or v_age < 300 then
    v_id := case when p_cancel then 'cancelled_' || p_session_id else v_first_id || ':unavailable' end;
    v_event := jsonb_build_object('event_id', v_id, 'sequence', v_sequence, 'session_id', p_session_id,
      'project_id', v_session.project_id, 'kind', case when p_cancel then 'cancelled' else 'connector_unavailable' end,
      'status', case when p_cancel then 'cancelled' else null end, 'message', null,
      'revision_id', null, 'validation', null, 'artifact_ids', '[]'::jsonb, 'design_outcome', null,
      'error', null, 'created_at', v_now);
    insert into public.opencode_events values
      (v_id, p_session_id, v_session.owner_user_id, v_session.project_id, v_sequence, v_event, v_now)
      on conflict (event_id) do nothing;
    if found then v_sequence := v_sequence + 1; end if;
  end if;
  update public.opencode_sessions set next_event_sequence = greatest(next_event_sequence, v_sequence)
    where session_id = p_session_id;
end;
$$;
revoke all on function public.reconcile_opencode_session(text, timestamptz, boolean) from public, anon, authenticated;
grant execute on function public.reconcile_opencode_session(text, timestamptz, boolean) to service_role;

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
  perform public.fail_stalled_opencode_commands(p_session_id, p_now);
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
