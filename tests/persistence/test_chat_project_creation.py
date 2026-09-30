from __future__ import annotations

import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from forma_core import database
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ProjectOverview


OWNER = "chat-project-owner"


@contextmanager
def sqlite_repository() -> Iterator[None]:
    with tempfile.TemporaryDirectory() as directory:
        provider = create_sqlite_provider(
            source="chat project creation test",
            url=f"sqlite:///{Path(directory) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        assert provider.session_factory is not None
        provider.initialize()
        original = database._DATABASE_REPOSITORY
        try:
            database._DATABASE_REPOSITORY = SqlAlchemyRepository(provider.session_factory)
            yield
        finally:
            database._DATABASE_REPOSITORY = original
            provider.dispose()


def project_state(project_id: str) -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation(
        overview=ProjectOverview(
            title="Chat sensor",
            description="A sensor generated from chat.",
            difficulty="Beginner",
            category="Sensors",
        ),
        assembly_metadata={"project_id": project_id},
    )


class ChatProjectCreationTests(unittest.TestCase):
    def test_bootstrap_is_identity_first_and_idempotent(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            first = database.ensure_chat_project(
                project_id,
                OWNER,
                prompt="Build a compact sensor.",
                chat_id="chat-1",
            )
            replay = database.ensure_chat_project(
                project_id,
                OWNER,
                prompt="A changed prompt must not replace the frozen brief.",
                chat_id="chat-2",
            )
            identities = database.list_project_identities(OWNER)
            briefs = database.list_design_brief_versions(project_id, OWNER)

        self.assertEqual(first.design_brief_id, replay.design_brief_id)
        self.assertEqual(1, len(identities))
        self.assertEqual(1, len(briefs))
        self.assertEqual("chat-1", identities[0]["chat_id"])

    def test_revision_replay_and_identity_recovery_are_idempotent(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            database.ensure_project_identity(project_id, OWNER, prompt="Build a sensor.", chat_id="chat-1")
            state = project_state(project_id)
            first = database.persist_chat_project_revision(
                project_id,
                OWNER,
                state,
                source_job_id="generation-1",
                prompt="Build a sensor.",
                chat_id="chat-1",
            )
            replay = database.persist_chat_project_revision(
                project_id,
                OWNER,
                state,
                source_job_id="generation-1",
                prompt="Build a sensor.",
                chat_id="chat-1",
            )
            latest = database.get_latest_project_revision(project_id, OWNER)
            projection = database.get_generated_project(project_id, include_deleted=True)

        self.assertEqual(1, first.revision)
        self.assertEqual(first.revision_id, replay.revision_id)
        self.assertEqual(first.revision_id, latest.revision_id)
        self.assertIsNotNone(projection)
        self.assertEqual(1, projection.hardware_ir["assembly_metadata"]["project_revision"])
