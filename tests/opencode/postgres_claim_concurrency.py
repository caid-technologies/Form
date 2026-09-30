"""Real Postgres row-lock tests. Run only against the disposable opencode_test DB."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
DSN = os.environ["OPENCODE_TEST_DATABASE_URL"]


def claim(session: str, token: str, at: datetime = NOW, *, name: str = "claim-test") -> list[tuple]:
    with psycopg.connect(DSN, application_name=name) as connection:
        connection.execute("SET ROLE service_role")
        connection.execute("SET statement_timeout = '8s'")
        return connection.execute(
            "SELECT command_id, attempt_count, lease_token_hash FROM public.claim_opencode_command(%s, %s, %s, 15, %s)",
            ("mini", session, token, at),
        ).fetchall()


def wait_for_lock(admin: psycopg.Connection, name: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if admin.execute("SELECT 1 FROM pg_stat_activity WHERE application_name = %s AND wait_event_type = 'Lock'", (name,)).fetchone():
            return
        time.sleep(0.02)
    raise AssertionError(f"Expected {name} to wait on a real row lock")


def main() -> None:
    with psycopg.connect(DSN, autocommit=True) as admin:
        if admin.execute("SELECT current_database()").fetchone()[0] != "opencode_test":
            raise RuntimeError("Use the disposable opencode_test database")
        for role in ["anon", "authenticated", "service_role"]:
            if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        for name in ["20260908000200_opencode_bridge.sql", "20260930152646_opencode_stale_sessions.sql", "20260930163207_opencode_serial_claims.sql"]:
            admin.execute((ROOT / "supabase/migrations" / name).read_text())
        admin.execute("GRANT SELECT, UPDATE ON public.opencode_sessions, public.opencode_commands TO service_role")
        for session in ["lock-a", "lock-b", "race", "renew"]:
            admin.execute("INSERT INTO public.opencode_sessions VALUES (%s, 'mini', 'owner', 'project', 'active', 'nonce', %s, %s, NULL, 1)",
                          (session, NOW.isoformat(), NOW.isoformat()))
            for index in range(3):
                admin.execute("""INSERT INTO public.opencode_commands
                    (command_id, session_id, connector_id, owner_user_id, project_id, operation, idempotency_key, status, message_digest, created_at, updated_at)
                    VALUES (%s, %s, 'mini', 'owner', 'project', 'project_message', %s, 'queued', 'digest', %s, %s)""",
                              (f"{session}-{index}", session, str(index), NOW.isoformat(), NOW.isoformat()))

        with ThreadPoolExecutor(max_workers=8) as pool:
            # A transaction holds the first claim open. Same-session claims must
            # block, while an unrelated session must finish before it commits.
            with psycopg.connect(DSN) as owner:
                owner.execute("SET ROLE service_role")
                first = owner.execute("SELECT command_id FROM public.claim_opencode_command('mini', 'lock-a', 'owner', 15, %s)", (NOW,)).fetchone()
                assert first == ("lock-a-0",)
                waiting = pool.submit(claim, "lock-a", "waiter", name="same-session-waiter")
                wait_for_lock(admin, "same-session-waiter")
                assert pool.submit(claim, "lock-b", "independent").result(timeout=3)[0][0] == "lock-b-0"
                assert not waiting.done()
            assert waiting.result(timeout=3) == []

            for attempt, at in [(1, NOW), (2, NOW + timedelta(seconds=15))]:
                barrier = Barrier(8)
                def competing(index: int) -> list[tuple]:
                    barrier.wait(timeout=5)
                    return claim("race", f"hash-{attempt}-{index}", at)
                rows = [row for result in pool.map(competing, range(8)) for row in result]
                assert len(rows) == 1, rows
                assert rows[0][:2] == ("race-0", attempt), rows

            # A heartbeat renewal wins its command-row lock before the claim.
            # The claimant must read the renewed row after waiting, not reclaim it.
            claim("renew", "original")
            with psycopg.connect(DSN) as heartbeat:
                heartbeat.execute("UPDATE public.opencode_commands SET status = 'running', lease_expires_at = %s WHERE command_id = 'renew-0'",
                                  ((NOW + timedelta(minutes=5)).isoformat(),))
                waiting = pool.submit(claim, "renew", "late", NOW + timedelta(seconds=15), name="renew-waiter")
                wait_for_lock(admin, "renew-waiter")
            assert waiting.result(timeout=3) == []
            assert admin.execute("SELECT status, attempt_count, lease_token_hash FROM public.opencode_commands WHERE command_id = 'renew-0'").fetchone() == ("running", 1, "original")
    print("Postgres concurrent claim, cross-session progress, reclaim, and heartbeat-lock tests passed")


if __name__ == "__main__":
    main()
