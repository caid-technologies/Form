-- Run before deploying the gateway. No scheduler or new configuration is needed:
-- owner reads and connector requests reconcile the durable session state.
create or replace function public.guard_opencode_command_session()
returns trigger language plpgsql security invoker set search_path = '' as $$
declare v_status text;
begin
  if TG_OP = 'INSERT' then
    select status into v_status from public.opencode_sessions
      where session_id = NEW.session_id for update;
    if v_status is distinct from 'active' then
      raise check_violation using message = 'OpenCode session is no longer active';
    end if;
  elsif OLD.status in ('succeeded', 'failed', 'cancelled') and NEW.status <> OLD.status then
    raise check_violation using message = 'OpenCode command is already terminal';
  end if;
  return NEW;
end;
$$;
drop trigger if exists guard_opencode_command_session on public.opencode_commands;
create trigger guard_opencode_command_session before insert or update on public.opencode_commands
  for each row execute function public.guard_opencode_command_session();
revoke all on function public.guard_opencode_command_session() from public, anon, authenticated;

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
