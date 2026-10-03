import os
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import HTTPException, Response

from apps.api.opencode_api import get_opencode_project_history
from apps.api.auth import UserContext
from forma_core.opencode.models import ConnectorEventInput, OpenCodeCommandStatus, OpenCodeOperation
from forma_core.opencode.public_events import project_public_event
from forma_core.opencode.store import OpenCodeStore


class ProjectHistoryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "history-test-key"}))
        ticks = count()
        self.enterContext(patch("forma_core.opencode.store._now", side_effect=lambda:
            datetime(2026, 9, 21, tzinfo=timezone.utc) + timedelta(seconds=next(ticks))))
        self.store = OpenCodeStore(":memory:")
        self.addCleanup(self.store.close)
        self.project_id = str(uuid4())

    def session(self, name, owner="owner", connector="mini", project=None):
        return self.store.create_session(session_id=name, connector_id=connector, owner_user_id=owner,
                                         project_id=project or self.project_id)

    def command(self, session, name, message):
        return self.store.create_command(command_id=name, session=session,
            operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key=name, message=message)

    def event(self, command, suffix, message, kind="assistant_message"):
        event = project_public_event(ConnectorEventInput(event_id=f"{command.command_id}:{suffix}",
            kind=kind, message=message), sequence=self.store.next_event_sequence(command.session_id),
            session_id=command.session_id, project_id=UUID(command.project_id),
            created_at=datetime.fromisoformat(command.created_at.replace("Z", "+00:00")) + timedelta(seconds=int(suffix)))
        self.store.add_event(event)

    def finish(self, command, status=OpenCodeCommandStatus.SUCCEEDED):
        claimed = self.store.claim_next(connector_id=command.connector_id, session_id=command.session_id)
        self.store.complete(self.store.get_command(command.command_id), claimed.lease_token, status)

    def test_recovers_private_turns_across_sessions_with_stable_ids_and_safe_replies(self):
        first = self.command(self.session("before-reload"), "first", "Make a plain gear with 36 rounded teeth.")
        self.event(first, "1", "Preparing the gear")
        self.event(first, "2", "The 36-tooth gear is saved.")
        self.event(first, "3", "tool: internal secret", kind="progress")
        self.finish(first)
        second = self.command(self.session("after-reload"), "second", "Render the same gear.")
        self.event(second, "1", "Image saved. token=secret-value")
        self.finish(second)
        foreign = self.command(self.session("foreign", owner="someone-else"), "foreign", "PRIVATE_OTHER_OWNER")
        self.finish(foreign)
        unrelated = self.command(self.session("other-project", project=str(uuid4())), "unrelated", "PRIVATE_OTHER_PROJECT")
        self.finish(unrelated)

        history = self.store.project_history(self.project_id, "owner")
        self.assertEqual(["first:user", "first:assistant", "second:user", "second:assistant"], [item.id for item in history])
        self.assertEqual(["Make a plain gear with 36 rounded teeth.", "The 36-tooth gear is saved.",
                          "Render the same gear.", "Image saved. [redacted]"], [item.content for item in history])
        self.assertEqual(history, self.store.project_history(self.project_id, "owner"))
        self.assertNotIn("ciphertext", str(history))
        self.assertNotIn("PRIVATE_OTHER", str(history))

    def test_failed_cancelled_and_active_requests_are_not_reported_as_successful(self):
        failed = self.command(self.session("failed-session"), "failed", "Try this")
        self.event(failed, "1", "Partial response")
        self.finish(failed, OpenCodeCommandStatus.FAILED)
        cancelled = self.command(self.session("cancelled-session"), "cancelled", "Stop this")
        self.store.cancel_command(cancelled)
        self.command(self.session("active-session"), "active", "Still queued")
        history = self.store.project_history(self.project_id, "owner")
        self.assertEqual(["idle", "error", "idle", "cancelled", "idle"], [item.status for item in history])

    def test_followup_context_survives_new_session_and_stays_owner_project_runtime_scoped(self):
        first = self.command(self.session("old"), "original", "Mechanical only; no electronics.")
        self.store.cancel_command(first)
        for name, kwargs in (("foreign", {"owner": "foreign"}), ("other-project", {"project": str(uuid4())}),
                             ("other-runtime", {"connector": "another-runtime"})):
            self.store.cancel_command(self.command(self.session(name, **kwargs), name, "UNRELATED"))
        fresh = self.session("new")
        self.command(fresh, "followup", "Generate an image")
        self.command(fresh, "future", "Add something later")
        claimed = self.store.claim_next(connector_id="mini", session_id="new")
        self.assertEqual(["Mechanical only; no electronics."], [item.message for item in claimed.conversation_context])

    def test_recovered_assistant_result_keeps_its_exact_revision_after_later_turns(self):
        session = self.session("revision-history")
        expected = []
        for index in range(2):
            command = self.command(session, f"revision-command-{index}", f"Change {index}")
            self.event(command, "1", f"Saved change {index}")
            saved_id = str(uuid4())
            expected.append(saved_id)
            completion = project_public_event(
                ConnectorEventInput(event_id=f"{command.command_id}:2", kind="completed"),
                sequence=self.store.next_event_sequence(session.session_id), session_id=session.session_id,
                project_id=UUID(self.project_id),
                created_at=datetime.fromisoformat(command.created_at.replace("Z", "+00:00")) + timedelta(seconds=2),
            ).model_copy(update={"revision_id": saved_id})
            self.store.add_event(completion)
            self.finish(command)
        messages = self.store.project_history(self.project_id, "owner")
        self.assertEqual([message.revisionId for message in messages if message.role == "assistant"], expected)
        self.assertTrue(all(message.revisionId is None for message in messages if message.role == "user"))

    def test_history_endpoint_checks_ownership_before_decrypting_any_messages(self):
        user = UserContext(provider="clerk", subject="owner", owner_user_id="owner",
                           is_authenticated=True, is_admin=False)
        with patch("apps.api.opencode_api._require_owned_project", side_effect=HTTPException(404)), \
             patch("apps.api.opencode_api.OPENCODE_STORE", self.store), \
             patch.object(self.store, "project_history") as read:
            with self.assertRaises(HTTPException):
                get_opencode_project_history(UUID(self.project_id), Response(), user)
            read.assert_not_called()
        with patch("apps.api.opencode_api._require_owned_project"), patch("apps.api.opencode_api.OPENCODE_STORE", self.store):
            response = Response()
            result = get_opencode_project_history(UUID(self.project_id), response, user)
            self.assertEqual((), result.messages)
            self.assertEqual("private, no-store", response.headers["Cache-Control"])
