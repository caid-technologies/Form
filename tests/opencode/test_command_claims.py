"""Provider-independent claim semantics and real concurrent SQLite callers."""
from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

from forma_core.opencode.models import OpenCodeCommandStatus, OpenCodeOperation
from forma_core.opencode.store import OpenCodeStore
from forma_core.persistence.providers import SupabaseProvider


class CommandClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}).start()
        self.now = datetime.now(timezone.utc)
        self.clock = patch("forma_core.opencode.store._now", return_value=self.now).start()
        self.addCleanup(patch.stopall)
        self.store = OpenCodeStore(":memory:")
        self.addCleanup(self.store.close)
        self.session = self.store.create_session(session_id="one", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
        for index in range(3):
            self.store.create_command(command_id=f"command-{index}", session=self.session,
                                      operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key=str(index), message=f"Request {index}")
            self.clock.return_value += timedelta(microseconds=1)

    def test_live_lease_blocks_later_work_and_completion_preserves_fifo(self) -> None:
        for index in range(3):
            claimed = self.store.claim_next(connector_id="mini", session_id="one")
            self.assertEqual(f"command-{index}", claimed.command_id)
            self.assertEqual(1, claimed.attempt_count)
            self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))
            command = self.store.heartbeat(self.store.get_command(claimed.command_id), claimed.lease_token)
            self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))
            self.store.complete(command, claimed.lease_token, OpenCodeCommandStatus.SUCCEEDED)
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))

    def test_expired_lease_is_reclaimed_before_later_queued_work(self) -> None:
        first = self.store.claim_next(connector_id="mini", session_id="one", lease_seconds=15)
        self.clock.return_value = first.lease_expires_at - timedelta(microseconds=1)
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))
        self.clock.return_value = first.lease_expires_at
        retried = self.store.claim_next(connector_id="mini", session_id="one")
        self.assertEqual(first.command_id, retried.command_id)
        self.assertEqual(2, retried.attempt_count)
        self.assertNotEqual(first.lease_token, retried.lease_token)
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))
        with self.assertRaises(PermissionError):
            self.store.heartbeat(self.store.get_command(first.command_id), first.lease_token)

    def test_polling_connector_cannot_reclaim_abandoned_work_forever(self) -> None:
        for attempt in range(1, 4):
            claimed = self.store.claim_next(connector_id="mini", session_id="one", lease_seconds=15)
            self.assertEqual("command-0", claimed.command_id)
            self.assertEqual(attempt, claimed.attempt_count)
            self.clock.return_value = claimed.lease_expires_at
            self.store.touch_session("one")  # Poll traffic is not command progress.
        following = self.store.claim_next(connector_id="mini", session_id="one")
        self.assertEqual("command-1", following.command_id)
        command = self.store.get_command("command-0")
        self.assertEqual(OpenCodeCommandStatus.FAILED, command.status)
        self.assertIsNone(command.lease_token_hash)
        with self.assertRaises(PermissionError):
            self.store.heartbeat(command, claimed.lease_token)
        self.store.reconcile_session("one")
        events = self.store.list_events("one", 0, 100)
        self.assertEqual(["command-0:terminal"], [event.event_id for event in events])
        self.assertEqual("opencode_command_stalled", events[0].error.code)
        self.assertEqual(2, self.store.next_event_sequence("one"))

    def test_owner_read_settles_exhausted_claim_once_but_preserves_healthy_long_work(self) -> None:
        for _ in range(3):
            claimed = self.store.claim_next(connector_id="mini", session_id="one", lease_seconds=15)
            self.clock.return_value = claimed.lease_expires_at
        self.store.reconcile_session("one")
        self.store.reconcile_session("one")
        self.assertEqual(1, len(self.store.list_events("one", 0, 100)))
        claimed = self.store.claim_next(connector_id="mini", session_id="one")
        for _ in range(60):
            self.clock.return_value += timedelta(seconds=30)
            self.store.heartbeat(self.store.get_command(claimed.command_id), claimed.lease_token)
            self.store.reconcile_session("one")
            self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))
        self.assertEqual(OpenCodeCommandStatus.RUNNING, self.store.get_command(claimed.command_id).status)

    def test_cancel_wins_over_exhausted_claim(self) -> None:
        for _ in range(3):
            claimed = self.store.claim_next(connector_id="mini", session_id="one", lease_seconds=15)
            self.clock.return_value = claimed.lease_expires_at
        self.store.reconcile_session("one", cancel=True)
        self.assertEqual(OpenCodeCommandStatus.CANCELLED, self.store.get_command("command-0").status)
        self.assertTrue(all(event.error is None for event in self.store.list_events("one", 0, 100)))

    def test_cancel_releases_next_command_and_other_sessions_are_independent(self) -> None:
        first = self.store.claim_next(connector_id="mini", session_id="one")
        other = self.store.create_session(session_id="two", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
        self.store.create_command(command_id="other", session=other, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="other", message="Other request")
        self.assertEqual("other", self.store.claim_next(connector_id="mini", session_id="two").command_id)
        self.store.cancel_command(self.store.get_command(first.command_id))
        self.assertEqual("command-1", self.store.claim_next(connector_id="mini", session_id="one").command_id)

    def test_any_live_lease_blocks_even_an_older_queued_command(self) -> None:
        with self.store._connection() as db:
            db.execute("UPDATE opencode_commands SET status = 'running', lease_expires_at = ? WHERE command_id = 'command-2'",
                       ((self.now + timedelta(seconds=60)).isoformat(),))
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))

    def test_wrong_connector_and_closed_session_cannot_claim(self) -> None:
        self.assertIsNone(self.store.claim_next(connector_id="other", session_id="one"))
        self.store.reconcile_session("one", cancel=True)
        self.assertIsNone(self.store.claim_next(connector_id="mini", session_id="one"))

    def test_supabase_uses_one_atomic_claim_rpc(self) -> None:
        command = self.store.get_command("command-0")
        provider = Mock(spec=SupabaseProvider)
        provider.client = Mock()
        provider.client.rpc.return_value.execute.return_value.data = [command.as_record()]
        with patch.object(self.store, "_ensure_provider", return_value=provider), patch.object(self.store, "_with_context", return_value="claimed"):
            self.assertEqual("claimed", self.store.claim_next(connector_id="mini", session_id="one"))
        args = provider.client.rpc.call_args.args
        self.assertEqual("claim_opencode_command", args[0])
        self.assertEqual("mini", args[1]["p_connector_id"])
        self.assertEqual("one", args[1]["p_session_id"])
        self.assertEqual(64, len(args[1]["p_lease_hash"]))
        provider.client.table.assert_not_called()

    def test_concurrent_store_instances_produce_one_live_claim_including_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "claims.sqlite")
            stores = [OpenCodeStore(path) for _ in range(8)]
            try:
                session = stores[0].create_session(session_id="race", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
                for index in range(4):
                    stores[0].create_command(command_id=f"race-{index}", session=session, operation=OpenCodeOperation.PROJECT_MESSAGE,
                                             idempotency_key=str(index), message="race")
                for store in stores:
                    store._ensure_provider()
                for attempt in [1, 2]:
                    barrier = Barrier(len(stores))
                    def claim(store: OpenCodeStore):
                        barrier.wait(timeout=5)
                        return store.claim_next(connector_id="mini", session_id="race", lease_seconds=15)
                    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
                        claimed = [result for result in pool.map(claim, stores) if result is not None]
                    self.assertEqual(1, len(claimed))
                    self.assertEqual("race-0", claimed[0].command_id)
                    self.assertEqual(attempt, claimed[0].attempt_count)
                    self.clock.return_value = claimed[0].lease_expires_at
            finally:
                for store in stores:
                    store.close()
