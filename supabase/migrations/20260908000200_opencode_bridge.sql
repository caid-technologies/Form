-- Durable hosted OpenCode connector protocol. Raw prompts and internal agent
-- events are intentionally not stored in these tables.

create table if not exists public.opencode_sessions (
  session_id text primary key,
  connector_id text not null,
  owner_user_id text not null,
  project_id text not null,
  status text not null,
  capability_nonce text not null,
  created_at text not null,
  updated_at text not null,
  last_heartbeat_at text,
  next_event_sequence integer not null default 1
);

create index if not exists idx_opencode_sessions_owner
  on public.opencode_sessions (owner_user_id);

create table if not exists public.opencode_commands (
  command_id text primary key,
  session_id text not null references public.opencode_sessions(session_id) on delete cascade,
  connector_id text not null,
  owner_user_id text not null,
  project_id text not null,
  operation text not null,
  idempotency_key text not null,
  status text not null,
  message_digest text not null,
  attempt_count integer not null default 0,
  lease_expires_at text,
  lease_token_hash text,
  created_at text not null,
  updated_at text not null,
  completed_at text,
  unique (session_id, idempotency_key)
);

create index if not exists idx_opencode_commands_queue
  on public.opencode_commands (connector_id, status, created_at);

create table if not exists public.opencode_events (
  event_id text primary key,
  session_id text not null references public.opencode_sessions(session_id) on delete cascade,
  owner_user_id text not null,
  project_id text not null,
  sequence integer not null,
  event_json jsonb not null,
  created_at text not null,
  unique (session_id, event_id),
  unique (session_id, sequence)
);

create index if not exists idx_opencode_events_session_sequence
  on public.opencode_events (session_id, sequence);
