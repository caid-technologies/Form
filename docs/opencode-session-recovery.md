# Recovering an offline Forma Agent request

## Remote inference diagnostics

The signed-in session owner can read
`GET /opencode/sessions/{session_id}/diagnostics`. Other owners receive 404;
connector capabilities do not authorize this owner endpoint. Responses are
`Cache-Control: private, no-store` and include the session status, connector/project
IDs, `last_successful_poll_at`, and `latest_failure` (null before any failure).
The contact timestamp is observed by the gateway and includes successful command
polls and session/command heartbeats; it is not a mini-PC clock or proof that
inference succeeded.

`latest_failure` includes the cloud receipt timestamp, gateway-bound command and
correlation IDs, fixed code/category, phase, retryability, and bounded provider/model
identifiers when available. It remains visible after later progress or successful
commands. Cancellation is included. Legacy failures without structured diagnostics
still show their public error code, including `connector_timeout` and
`opencode_lease_expired`, so an offline connector can be distinguished from a
provider authentication, model, rate-limit, or timeout failure. Reading diagnostics
also reconciles stale sessions using the timeout policy below.

The connector sends the same optional diagnostic with its terminal event and
completion request. Completion persists it if event delivery failed; the canonical
terminal event is immutable on retries. Deploy this receiver before the paired
connector diagnostics update. Older connectors may omit the new fields. There is
no new table, retention policy, or raw-log store: diagnostics live in the existing
session event JSON and follow its lifecycle. Provider responses, exception text,
prompts, credentials, file contents, and host logs are excluded. Detailed host logs
remain on the mini-PC as documented in `local-server-config/docs/opencode-connector.md`.

## Session recovery

An outstanding request receives a connector-unavailable warning after two minutes
without contact. Stop remains available. If contact resumes before five minutes,
the same request can continue. Healthy long-running requests are not subject to a
five-minute total runtime limit.

After five minutes without contact, the gateway atomically marks the session and
its outstanding commands failed, clears their leases, and records one canonical
failure event per command. The public diagnostic code is `connector_timeout`.
The conversation stops loading and offers **Try failed build again**. This retries
the original message in a new session with a new command idempotency key; repeated
submission of that key within its session still returns the same command. Existing
saved projects remain linked. A request that never saved a project receives a new
reserved project ID on retry.

Timeouts are reconciled on owner session/event reads, command submission, connector
discovery, and connector requests. No background scheduler is required. With no
traffic, the persisted transition happens on the next relevant request. A late
connector reconciles the timeout before renewing contact, and failed sessions cannot
accept new commands, MCP tool requests, completion, or event delivery. Existing
saved revisions are retained; timeout does not roll back work saved before the outage.

An idle conversation with no outstanding work does not time out. Its next command
gets a fresh grace period. Cancellation is persisted even when the connector is
offline. Timeout and cancellation updates, lease revocation, and failure events
share a database transaction.

## Rollout

Apply `supabase/migrations/20260930152646_opencode_stale_sessions.sql` before deploying
the hosted backend. The migration adds a backend-only reconciliation function and
a command-state guard; no new environment variables are needed. SQLite implements
the same transition in a write transaction without a migration. Deploy the frontend
for the retry action. No connector protocol change is required.

## Verification

- Python: `python -m pytest tests/opencode -q`
- Frontend: `node --experimental-strip-types --test test/opencode-turn.test.ts` from `apps/web`
- Browser: `npx playwright test --config playwright.integrity.config.ts --grep 'OpenCode outage'`
- Disposable Postgres: `psql -v ON_ERROR_STOP=1 -f tests/opencode/stale_sessions.sql`

The SQL test creates test roles/tables and rolls back. Run it only in a disposable
database. Browser fixtures simulate connector events; a deployed mini-PC smoke test
remains a rollout check.

## Heartbeat and lease failures

The gateway returns `detail.code`, a fixed public message, and a correlation ID.
The connector uses the code, never exception text or response bodies:

| Code | Connector behavior |
| --- | --- |
| `opencode_capability_invalid`, `opencode_capability_expired` | Stop; do not retry rejected authorization. |
| `opencode_scope_mismatch` | Stop; do not refresh credentials to bypass the scope check. |
| `opencode_lease_invalid` | Stop this worker; a newer claimant may own the command. |
| `opencode_lease_expired` | Stop; the gateway atomically fails the matching expired command and writes its canonical failure event. |
| `opencode_command_closed`, `opencode_session_closed` | Stop; never revive terminal work. |
| `opencode_transport_unavailable` | Connector-generated category for network/timeouts, HTTP 408/425/429, and 5xx; retry within the last confirmed lease window. |

Missing credentials (`opencode_capability_required`, `opencode_lease_required`,
`opencode_connector_auth_required`) and other 4xx responses are also terminal.
Malformed successful responses are protocol failures, not authorization failures.
All public messages are fixed; tokens, prompts, remote paths, response bodies,
and raw exceptions are excluded from diagnostics.

The paired `local-server-config` connector change renews its local deadline on
successful heartbeats and uses bounded exponential backoff (250ms to 2s). Each HTTP
request has a timeout no longer than the remaining lease or 10 seconds. It retries
events with the same event ID and completion with the same status and lease token;
a lost acknowledgement cannot duplicate the durable result. Already acknowledged
events remain replayable within their authorized active session after completion;
new events still require a valid lease. A wrong or superseded lease cannot fail a
new claimant. Authorization failures cannot mutate the command; the existing
five-minute session outage reconciliation remains the fallback when an authorized
terminal write is impossible. The connector stops polling a rejected session
instead of immediately refreshing credentials and reclaiming the same work;
discovery still runs the gateway timeout reconciliation.

Apply `supabase/migrations/20260930160740_opencode_lease_failures.sql` before this
backend release, after the stale-session migration. Deploy the gateway before the
paired connector update for specific failure categories. The new connector still
stops on an older gateway's generic 403. No environment changes are required.
Validate the additional RPC with `psql -v ON_ERROR_STOP=1 -f tests/opencode/lease_failures.sql`
in a disposable database. Real mini-PC reconnect testing remains a rollout check.

## Concurrent sessions and serialized command claims

A connector may run separate sessions concurrently, but each connector/session pair
can hold at most one unexpired command lease. Later queued work stays queued until
the current command completes, is cancelled, or its lease expires. At expiry the
oldest eligible command is reclaimed with a fresh token and incremented attempt
count. FIFO order uses `created_at` with `command_id` as a deterministic tie-breaker.

Supabase performs the check and claim inside `claim_opencode_command`, locking the
parent session and then its pending command rows. This coordinates concurrent
pollers, lease renewals, cancellation, and timeout reconciliation without blocking
unrelated sessions. SQLite performs the same check and update in one immediate
write transaction. Command submission still returns immediately; no request waits
for authoring to complete.

Apply `supabase/migrations/20260930163207_opencode_serial_claims.sql` before deploying
the gateway, then update all gateway instances before enabling the connector's
continuous scheduler (local-server-config #61). The migration adds one partial
queue index and one backend-only function; existing commands are preserved. No new
environment variable is required. `FORMA_MAX_OPENCODE_INSTANCES` remains the local
capacity setting and is shared by the scheduler and process supervisor.

Verification includes `tests/opencode/test_command_claims.py`, the disposable SQL
fixture `tests/opencode/serial_claims.sql`, and a real Postgres multi-connection test
`tests/opencode/postgres_claim_concurrency.py`. CI runs the latter against its
`opencode_test` service to prove same-session blocking, independent-session progress,
concurrent expired-lease reclaim, and a heartbeat renewal racing a claim.
