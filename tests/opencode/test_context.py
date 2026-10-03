import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch
from uuid import uuid4

from forma_core.opencode.models import OpenCodeOperation
from forma_core.opencode.store import OpenCodeStore


class ContextTests(unittest.TestCase):
    def setUp(self):
        # Model sequential chat requests independently of the OS clock resolution.
        ticks = count()
        self.enterContext(patch("forma_core.opencode.store._now", side_effect=lambda:
            datetime(2026, 9, 14, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))))

    def test_cancelled_brief_survives_restart_and_excludes_other_sessions_and_future_requests(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}):
            path = directory + "/store.sqlite"
            store = OpenCodeStore(path)
            session = store.create_session(session_id="chat", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
            other = store.create_session(session_id="other", connector_id="mini", owner_user_id="other-owner", project_id=str(uuid4()))
            def create(target, identifier, message):
                return store.create_command(command_id=identifier, session=target, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key=identifier, message=message)
            first = create(session, "first", "20 mm cube with X Y Z")
            store.cancel_command(first)
            create(other, "foreign", "private other project")
            create(session, "continue", "continue")
            create(session, "future", "future edit")
            store.close()
            reopened = OpenCodeStore(path)
            try:
                command = reopened.claim_next(connector_id="mini", session_id="chat")
                self.assertEqual(command.message, "continue")
                self.assertEqual([(item.message, item.status.value) for item in command.conversation_context], [("20 mm cube with X Y Z", "cancelled")])
                self.assertNotIn("20 mm cube", str(reopened.get_command("first").as_record()))
                self.assertEqual([], reopened.list_events("chat", 0, 100))
            finally:
                reopened.close()

    def test_long_conversation_retains_original_brief_and_recent_requests(self):
        with patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "test-key"}):
            store = OpenCodeStore(":memory:")
            self.addCleanup(store.close)
            session = store.create_session(session_id="chat", connector_id="mini", owner_user_id="owner", project_id=str(uuid4()))
            for i in range(25):
                command = store.create_command(command_id=str(i), session=session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key=str(i), message=f"request {i}")
                if i < 24:
                    store.cancel_command(command)
            claimed = store.claim_next(connector_id="mini", session_id="chat")
            self.assertEqual([item.message for item in claimed.conversation_context], ["request 0"] + [f"request {i}" for i in range(9, 24)])
