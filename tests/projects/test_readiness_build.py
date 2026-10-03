from __future__ import annotations

import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from apps.api.auth import UserContext, require_user_context
from apps.api.readiness_api import router
from forma_core import database
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workspaces.design_briefs import DesignBriefCreate
from forma_core.workspaces.readiness import BuildMode, ReadinessError, ReadinessStatus


OWNER = "readiness-user"
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
            source="readiness test",
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


def brief_payload(
    *,
    outputs: list[str] | None = None,
    validation: list[str] | None = None,
    questions: list[str] | None = None,
) -> DesignBriefCreate:
    return DesignBriefCreate.model_validate({
        "schema_version": "1.0",
        "conversation_id": "readiness-chat",
        "intent": "Design a compact environmental monitor",
        "summary": "USB-C environmental monitor with an OLED display",
        "requirements": ["Measure temperature and humidity", "Display the latest readings"],
        "constraints": ["Use USB-C 5 V power", "Fit within a 100 mm enclosure"],
        "references": [],
        "requested_outputs": ["wiring", "bom"] if outputs is None else outputs,
        "validation_criteria": ["Readings remain within sensor tolerance"] if validation is None else validation,
        "unresolved_questions": [] if questions is None else questions,
        "assumptions": [],
        "readiness": "draft",
    })


def create_project_context(project_id: str, brief: DesignBriefCreate) -> None:
    database.initialize_project_workflow(project_id, OWNER)
    database.create_design_brief_version(project_id, OWNER, brief)


class ReadinessEvaluationTests(unittest.TestCase):
    def test_ready_incomplete_and_critical_results_explain_their_blockers(self) -> None:
        ready_id = str(uuid.uuid4())
        incomplete_id = str(uuid.uuid4())
        blocked_id = str(uuid.uuid4())
        with sqlite_repository():
            create_project_context(ready_id, brief_payload())
            create_project_context(incomplete_id, brief_payload(outputs=[], validation=[]))
            create_project_context(
                blocked_id,
                brief_payload(questions=["What voltage and maximum current must the power rail support?"]),
            )
            ready = database.evaluate_project_readiness(ready_id, OWNER)
            incomplete = database.evaluate_project_readiness(incomplete_id, OWNER)
            blocked = database.evaluate_project_readiness(blocked_id, OWNER)

        self.assertEqual(ReadinessStatus.READY, ready.status)
        self.assertEqual(ReadinessStatus.NOT_READY, incomplete.status)
        self.assertEqual({"requested_outputs_missing", "validation_criteria_missing"}, {
            blocker.code for blocker in incomplete.unresolved_blockers
        })
        self.assertEqual(ReadinessStatus.BLOCKED, blocked.status)
        self.assertTrue(blocked.unresolved_blockers[0].critical)
        self.assertEqual("electrical", blocked.unresolved_blockers[0].category.value)

    def test_every_non_bypassable_unknown_category_is_classified_as_critical(self) -> None:
        questions = {
            "safety": "What safety protection and emergency limits are required?",
            "dimensional": "What enclosure dimensions and mounting tolerances are required?",
            "electrical": "What voltage and maximum current must the power rail support?",
            "material": "What material and operating temperature are required?",
            "manufacturing": "What fabrication process and manufacturing tooling are available?",
        }
        with sqlite_repository():
            for expected_category, question in questions.items():
                with self.subTest(category=expected_category):
                    project_id = str(uuid.uuid4())
                    create_project_context(project_id, brief_payload(questions=[question]))
                    result = database.evaluate_project_readiness(project_id, OWNER)
                    self.assertEqual(ReadinessStatus.BLOCKED, result.status)
                    self.assertTrue(result.unresolved_blockers[0].critical)
                    self.assertEqual(expected_category, result.unresolved_blockers[0].category.value)


class BuildSemanticsTests(unittest.TestCase):
    def test_build_freezes_exact_ready_brief_and_enters_building(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            create_project_context(project_id, brief_payload())
            outcome = database.initiate_project_build(
                project_id,
                OWNER,
                mode=BuildMode.BUILD,
                actor_id=OWNER,
                idempotency_key="ready-build",
            )
            persisted = database.get_latest_project_build(project_id, OWNER)
            history = database.list_project_workflow_transitions(project_id, OWNER)

        self.assertEqual("building", outcome.workflow.state.value)
        self.assertEqual("build", outcome.build.mode.value)
        self.assertEqual(1, outcome.build.brief_version)
        self.assertEqual(outcome.build.brief_snapshot, persisted.brief_snapshot)
        self.assertEqual(outcome.build.design_brief_id, outcome.build.brief_snapshot.design_brief_id)
        self.assertEqual(["gathering_context", "building"], [item.to_state.value for item in history])

    def test_build_rejects_incomplete_brief_without_persisting_or_transitioning(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            create_project_context(project_id, brief_payload(outputs=[]))
            with self.assertRaises(ReadinessError) as raised:
                database.initiate_project_build(
                    project_id,
                    OWNER,
                    mode=BuildMode.BUILD,
                    actor_id=OWNER,
                )
            workflow = database.get_project_workflow(project_id, OWNER)
            with self.assertRaises(ReadinessError):
                database.get_latest_project_build(project_id, OWNER)

        self.assertEqual("readiness_not_ready", raised.exception.code)
        self.assertEqual("gathering_context", workflow.state.value)

    def test_build_anyway_persists_assumptions_warnings_and_the_new_frozen_version(self) -> None:
        project_id = str(uuid.uuid4())
        assumptions = ["Produce wiring and a BOM in the first build", "Validate against a bench prototype"]
        with sqlite_repository():
            create_project_context(project_id, brief_payload(outputs=[], validation=[]))
            first = database.initiate_project_build(
                project_id,
                OWNER,
                mode=BuildMode.BUILD_ANYWAY,
                actor_id=OWNER,
                assumptions=assumptions,
                idempotency_key="build-anyway-1",
            )
            replay = database.initiate_project_build(
                project_id,
                OWNER,
                mode=BuildMode.BUILD_ANYWAY,
                actor_id=OWNER,
                assumptions=assumptions,
                idempotency_key="build-anyway-1",
            )
            versions = database.list_design_brief_versions(project_id, OWNER)

        self.assertEqual(2, first.build.brief_version)
        self.assertEqual(2, first.build.readiness.brief_version)
        self.assertEqual(assumptions, first.build.introduced_assumptions)
        self.assertEqual(assumptions, first.build.brief_snapshot.assumptions)
        self.assertTrue(any("requested_outputs_missing" in warning for warning in first.build.warnings))
        self.assertTrue(any("validation_criteria_missing" in warning for warning in first.build.warnings))
        self.assertEqual([1, 2], [brief.brief_version for brief in versions])
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(first.build.build_id, replay.build.build_id)

    def test_build_anyway_cannot_bypass_critical_unknowns(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            create_project_context(
                project_id,
                brief_payload(questions=["What enclosure dimensions and electrical clearances are required?"]),
            )
            with self.assertRaises(ReadinessError) as raised:
                database.initiate_project_build(
                    project_id,
                    OWNER,
                    mode=BuildMode.BUILD_ANYWAY,
                    actor_id=OWNER,
                    assumptions=["Use common prototype dimensions"],
                )
            workflow = database.get_project_workflow(project_id, OWNER)
            versions = database.list_design_brief_versions(project_id, OWNER)

        self.assertEqual("critical_readiness_blockers", raised.exception.code)
        self.assertEqual("gathering_context", workflow.state.value)
        self.assertEqual(1, len(versions))

    def test_conversational_build_can_delegate_unanswered_choices_to_agents(self) -> None:
        project_id = str(uuid.uuid4())
        question = "Which controller and major modules should be treated as fixed?"
        with sqlite_repository():
            create_project_context(project_id, brief_payload(questions=[question]))
            outcome = database.initiate_project_build(
                project_id,
                OWNER,
                mode=BuildMode.BUILD_ANYWAY,
                actor_id=OWNER,
                assumptions=["Use safe prototype defaults and record them."],
                resolve_unanswered_questions=True,
            )
            versions = database.list_design_brief_versions(project_id, OWNER)

        self.assertEqual("building", outcome.workflow.state.value)
        self.assertEqual([], outcome.build.brief_snapshot.unresolved_questions)
        self.assertTrue(any(question in item for item in outcome.build.introduced_assumptions))
        self.assertTrue(any("delegated blocker" in warning for warning in outcome.build.warnings))
        self.assertEqual([1, 2], [brief.brief_version for brief in versions])


class ReadinessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user_context] = lambda: USER
        self.client = TestClient(app)

    def test_all_routes_require_authenticated_context(self) -> None:
        for route in (route for route in router.routes if isinstance(route, APIRoute)):
            self.assertIn(require_user_context, {dependency.call for dependency in route.dependant.dependencies})

    def test_api_reports_readiness_rejects_build_and_accepts_build_anyway(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            create_project_context(project_id, brief_payload(outputs=[]))
            readiness = self.client.get(f"/projects/{project_id}/readiness")
            rejected = self.client.post(f"/projects/{project_id}/build", json={})
            built = self.client.post(
                f"/projects/{project_id}/build-anyway",
                json={"assumptions": ["Return a wiring plan first"], "idempotency_key": "api-build-anyway"},
            )
            frozen = self.client.get(f"/projects/{project_id}/build")

        self.assertEqual(200, readiness.status_code)
        self.assertEqual("not_ready", readiness.json()["status"])
        self.assertEqual(409, rejected.status_code)
        self.assertEqual("readiness_not_ready", rejected.json()["detail"]["code"])
        self.assertEqual(200, built.status_code, built.text)
        self.assertEqual("building", built.json()["workflow"]["state"])
        self.assertEqual(200, frozen.status_code)
        self.assertEqual(2, frozen.json()["brief_version"])


if __name__ == "__main__":
    unittest.main()
