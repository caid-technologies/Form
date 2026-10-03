# Recovering an offline Forma Agent request

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

## Stalled claims and browser status recovery

Connector discovery/poll traffic proves connectivity, but does not prove that an
individual command started. A command may be claimed at most three times. Once
its third lease expires, the next owner read or connector claim atomically fails
that command with `opencode_command_stalled`, clears its lease, and records one
`<command_id>:terminal` event. A connector that keeps polling cannot reset this
budget. The session remains active and later queued commands can run. A command
with a healthy renewed lease can continue indefinitely, including on its third
attempt. Cancellation wins if it settles first; late heartbeats, completion, and
events cannot revive terminal work.

Browser event requests have a 10-second deadline covering authentication headers,
fetch, and response parsing. After three consecutive failures (3 seconds between
retries), local loading stops. A successful poll resets that counter. The message
says the outcome is unknown and offers **Check request status**. This action only
reads events for the existing command; it never submits another command or cancels
remote work. The session/command IDs, event cursor, original prompt, and partial
reply are persisted with chat history so the same action works after a reload.
Confirmed failures offer **Try failed build again**. Partial assistant text and
existing project links remain visible. Stop, unmount, and account changes abort
polling; responses from an old run cannot update a newer one. Post-completion
project loading is also bounded, independently of command success.

Apply `supabase/migrations/20261003142232_opencode_stalled_commands.sql` after the
previous OpenCode migrations and before deploying this backend/frontend release.
It replaces the claim and reconciliation functions and adds one backend-only
invoker helper. Existing commands already at three or more expired attempts settle
on their next read/claim. No connector change or environment variable is required.
The connector's original failure still needs its own diagnosis; this fix bounds
its effect on the gateway and chat UI without weakening identity or lease checks.

Additional regressions:

- `tests/opencode/stalled_commands.sql` (disposable PostgreSQL, including role grants).
- `tests/opencode/postgres_claim_concurrency.py` (concurrent claim exhaustion).
- `node --experimental-strip-types --test test/opencode-turn.test.ts test/opencode-polling.test.ts` in `apps/web`.
- `npx playwright test --config playwright.integrity.config.ts --grep 'OpenCode polling recovery'` in `apps/web`.

Diagnostics include claim attempt counts, terminal event IDs/error codes, and
browser polling failure categories/counts. They exclude credentials, prompts,
provider responses, and raw exceptions.
