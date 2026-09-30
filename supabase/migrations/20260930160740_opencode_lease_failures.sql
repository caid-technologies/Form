-- Rejecting an expired lease must settle the command and its public event together.
create or replace function public.fail_expired_opencode_lease(
  p_command_id text, p_lease_hash text, p_now timestamptz default now()
) returns void language plpgsql security invoker set search_path = '' as $$
declare
  v_session public.opencode_sessions%rowtype;
  v_command public.opencode_commands%rowtype;
  v_sequence integer;
  v_id text := p_command_id || ':terminal';
  v_now text := to_char(p_now at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
begin
  -- Follow reconciliation's lock order: session, then command.
  select s.* into v_session from public.opencode_sessions s
    join public.opencode_commands c on c.session_id = s.session_id
    where c.command_id = p_command_id for update of s;
  if not found then return; end if;
  select * into v_command from public.opencode_commands where command_id = p_command_id for update;
  if v_command.status not in ('leased', 'running')
    or v_command.lease_token_hash is distinct from p_lease_hash
    or p_lease_hash is null
    or v_command.lease_expires_at::timestamptz > p_now then return; end if;
  select greatest(v_session.next_event_sequence, coalesce(max(sequence), 0) + 1)
    into v_sequence from public.opencode_events where session_id = v_session.session_id;
  update public.opencode_commands set status = 'failed', completed_at = v_now, updated_at = v_now,
    lease_token_hash = null, lease_expires_at = null where command_id = p_command_id;
  insert into public.opencode_events (event_id, session_id, owner_user_id, project_id, sequence, event_json, created_at)
    values (v_id, v_session.session_id, v_session.owner_user_id, v_session.project_id, v_sequence,
      jsonb_build_object('event_id', v_id, 'sequence', v_sequence, 'session_id', v_session.session_id,
        'project_id', v_session.project_id, 'kind', 'failed', 'status', 'failed', 'message', null,
        'revision_id', null, 'validation', null, 'artifact_ids', '[]'::jsonb, 'design_outcome', null,
        'created_at', v_now, 'error', jsonb_build_object('code', 'opencode_lease_expired',
          'message', 'Forma Agent lost its command lease. Submit the request again.', 'correlation_id', 'event_' || v_id)), v_now)
    on conflict (event_id) do nothing;
  if found then
    update public.opencode_sessions set next_event_sequence = v_sequence + 1 where session_id = v_session.session_id;
  end if;
end;
$$;
revoke all on function public.fail_expired_opencode_lease(text, text, timestamptz) from public, anon, authenticated;
grant execute on function public.fail_expired_opencode_lease(text, text, timestamptz) to service_role;
