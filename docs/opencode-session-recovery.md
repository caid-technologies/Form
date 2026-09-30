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
