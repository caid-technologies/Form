from __future__ import annotations

import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.auth import UserContext, require_user_context
from apps.api.project_workflow_api import router
from forma_core import database
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workspaces.workflow import (
    ALLOWED_WORKFLOW_TRANSITIONS,
    ProjectWorkflowService,
    ProjectWorkflowState,
    WorkflowActorType,
    WorkflowStateError,
)


OWNER = "workflow-user"
USER = UserContext(
    provider="test",
    subject=OWNER,
    owner_user_id=OWNER,
    is_authenticated=True,
    is_admin=False,
)


@contextmanager
def sqlite_repository() -> Iterator[None]:
    with tempfile.TemporaryDirectory() as directory:
        provider = create_sqlite_provider(
            source="workflow test",
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


class StateRepository:
    def __init__(self, state: ProjectWorkflowState) -> None:
        self.workflow = SimpleNamespace(
            project_id=str(uuid.uuid4()),
            owner_user_id=OWNER,
            state=state.value,
            revision=3,
            created_at="2026-08-01T12:00:00Z",
            updated_at="2026-08-01T12:01:00Z",
        )

    def get_project_workflow(self, project_id: str, owner_user_id: str | None) -> Any:
        if project_id != self.workflow.project_id:
            return None
        if owner_user_id and owner_user_id != self.workflow.owner_user_id:
            return None
        return self.workflow

    def list_project_workflow_transitions(self, project_id: str, owner_user_id: str) -> list[Any]:
        return []

    def get_project_workflow_transition_by_idempotency(
        self, project_id: str, owner_user_id: str, idempotency_key: str
    ) -> None:
        return None

    def apply_project_workflow_transition(
        self,
        state_record: dict[str, Any],
        transition_record: dict[str, Any],
        expected_state: str | None,
        expected_revision: int | None,
    ) -> tuple[Any, Any] | None:
        if self.workflow.state != expected_state or self.workflow.revision != expected_revision:
            return None
        self.workflow = SimpleNamespace(**state_record)
        return self.workflow, SimpleNamespace(**transition_record)


class WorkflowTransitionMatrixTests(unittest.TestCase):
    def test_every_allowed_and_rejected_transition(self) -> None:
        states = list(ProjectWorkflowState)
        for source in states:
            for target in states:
                with self.subTest(source=source.value, target=target.value):
                    repository = StateRepository(source)
                    service = ProjectWorkflowService(repository)
                    if source == target:
                        outcome = service.transition(
                            repository.workflow.project_id,
                            OWNER,
                            target,
                            actor_type=WorkflowActorType.USER,
                            actor_id=OWNER,
                            reason="Repeat current state.",
                        )
                        self.assertTrue(outcome.idempotent_replay)
                    elif target in ALLOWED_WORKFLOW_TRANSITIONS[source]:
                        outcome = service.transition(
                            repository.workflow.project_id,
                            OWNER,
                            target,
                            actor_type=WorkflowActorType.USER,
                            actor_id=OWNER,
                            reason="Exercise allowed transition.",
                        )
                        self.assertEqual(target, outcome.workflow.state)
                        self.assertEqual(source, outcome.transition.from_state)
                    else:
                        with self.assertRaises(WorkflowStateError) as raised:
                            service.transition(
                                repository.workflow.project_id,
                                OWNER,
                                target,
                                actor_type=WorkflowActorType.USER,
                                actor_id=OWNER,
                                reason="Exercise rejected transition.",
                            )
                        self.assertEqual("invalid_workflow_transition", raised.exception.code)


class WorkflowPersistenceTests(unittest.TestCase):
    def test_initialization_history_and_idempotent_transition_are_persisted(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            initial = database.initialize_project_workflow(project_id, OWNER, actor_id=OWNER)
            repeated_initial = database.initialize_project_workflow(project_id, OWNER, actor_id=OWNER)
            ready = database.transition_project_workflow(
                project_id,
                OWNER,
                ProjectWorkflowState.READY_TO_BUILD,
                actor_type=WorkflowActorType.USER,
                actor_id=OWNER,
                reason="Minimum context is present.",
                idempotency_key="ready-request-1",
            )
            replay = database.transition_project_workflow(
                project_id,
                OWNER,
                ProjectWorkflowState.READY_TO_BUILD,
                actor_type=WorkflowActorType.USER,
                actor_id=OWNER,
                reason="Minimum context is present.",
                idempotency_key="ready-request-1",
            )
            current = database.get_project_workflow(project_id, OWNER)
            history = database.list_project_workflow_transitions(project_id, OWNER)

        self.assertEqual(ProjectWorkflowState.GATHERING_CONTEXT, initial.workflow.state)
        self.assertEqual(1, initial.workflow.revision)
        self.assertTrue(repeated_initial.idempotent_replay)
        self.assertEqual(ProjectWorkflowState.READY_TO_BUILD, current.state)
        self.assertEqual(2, current.revision)
        self.assertEqual(2, ready.workflow.revision)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(ready.transition.transition_id, replay.transition.transition_id)
        self.assertEqual([1, 2], [transition.revision for transition in history])
        self.assertEqual(OWNER, history[1].actor_id)
        self.assertEqual("Minimum context is present.", history[1].reason)

    def test_owner_scope_and_generated_project_claim_use_the_same_identity(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            database.initialize_project_workflow(project_id, OWNER)
            with self.assertRaises(WorkflowStateError):
                database.get_project_workflow(project_id, "other-user")
            with self.assertRaises(database.DesignBriefAccessError):
                database.save_generated_project(
                    project_id=project_id,
                    title="Wrong owner",
                    prompt="Do not save",
                    hardware_ir={},
                    created_at="2026-08-01T12:00:00Z",
                    owner_user_id="other-user",
                )


class WorkflowApiTests(unittest.TestCase):
    def test_api_starts_in_gathering_context_and_returns_structured_rejection(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user_context] = lambda: USER
        client = TestClient(app)
        project_id = str(uuid.uuid4())

        with sqlite_repository():
            initialized = client.post(f"/projects/{project_id}/workflow")
            rejected = client.post(
                f"/projects/{project_id}/workflow/transitions",
                json={"to_state": "completed", "reason": "Invalid shortcut"},
            )
            history = client.get(f"/projects/{project_id}/workflow/transitions")

        self.assertEqual(200, initialized.status_code)
        self.assertEqual("gathering_context", initialized.json()["workflow"]["state"])
        self.assertEqual(409, rejected.status_code)
        self.assertEqual("invalid_workflow_transition", rejected.json()["detail"]["code"])
        self.assertEqual(1, len(history.json()["transitions"]))


if __name__ == "__main__":
    unittest.main()
