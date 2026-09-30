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
