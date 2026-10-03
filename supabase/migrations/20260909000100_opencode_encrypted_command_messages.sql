-- Store queued OpenCode prompts durably without exposing raw prompt text at rest.
alter table public.opencode_commands
  add column if not exists message_ciphertext text,
  add column if not exists message_key_id text;
