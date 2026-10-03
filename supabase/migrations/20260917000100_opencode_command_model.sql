-- Null preserves the connector's OpenCode default. Explicit choices belong to
-- the command, so queued work and retries cannot inherit a later UI selection.
ALTER TABLE public.opencode_commands ADD COLUMN IF NOT EXISTS model varchar(200);
