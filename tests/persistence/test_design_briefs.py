from __future__ import annotations

import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.api.auth import UserContext, require_user_context
from apps.api.design_briefs_api import router
from forma_core import database
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workspaces.design_briefs import (
    DesignBrief,
    DesignBriefCreate,
    DesignBriefReadiness,
    prompt_safe_design_brief,
)


USER_CONTEXT = UserContext(
    provider="test",
    subject="user_design_brief",
    owner_user_id="user_design_brief",
    is_authenticated=True,
    is_admin=False,
)


@contextmanager
def sqlite_repository() -> Iterator[None]:
    with tempfile.TemporaryDirectory() as directory:
        provider = create_sqlite_provider(
            source="design brief test",
            url=f"sqlite:///{Path(directory) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        assert provider.session_factory is not None
        provider.initialize()
        original_repository = database._DATABASE_REPOSITORY
        try:
            database._DATABASE_REPOSITORY = SqlAlchemyRepository(provider.session_factory)
            yield
        finally:
            database._DATABASE_REPOSITORY = original_repository
            provider.dispose()


def brief_payload(*, summary: str = "A compact controller") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "conversation_id": "chat-design-1",
        "intent": "Design a low-voltage motor controller",
        "summary": summary,
        "requirements": ["Drive one brushed DC motor", "Expose speed control"],
        "constraints": ["Use USB-C power", "Fit within 100 mm"],
        "references": [
            {
                "reference_id": "uploaded-hardware-reference",
                "kind": "uploaded_image",
                "label": "Existing enclosure",
                "uri": "s3://design-inputs/enclosure.png",
                "media_type": "image/png",
                "metadata": {"source": "clipboard"},
            }
        ],
        "requested_outputs": ["wiring", "bom", "enclosure"],
        "validation_criteria": ["Motor reaches requested speed"],
        "unresolved_questions": ["What is the stall current?"],
        "assumptions": ["The motor is rated for 12 V"],
        "readiness": "needs_clarification",
    }


class DesignBriefModelTests(unittest.TestCase):
    def test_v1_round_trips_every_canonical_field(self) -> None:
        project_id = uuid.uuid4()
        brief = DesignBrief(
            **brief_payload(),
            design_brief_id=uuid.uuid4(),
            project_id=project_id,
            brief_version=1,
            created_at="2026-08-01T18:00:00Z",
        )

        restored = DesignBrief.model_validate_json(brief.model_dump_json())

        self.assertEqual(brief, restored)
        self.assertEqual(project_id, restored.project_id)
        self.assertEqual("clipboard", restored.references[0].metadata["source"])
        self.assertEqual(DesignBriefReadiness.NEEDS_CLARIFICATION, restored.readiness)

    def test_prompt_safe_brief_strips_inline_image_payloads(self) -> None:
        brief = DesignBrief(
            **brief_payload(),
            design_brief_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            brief_version=1,
            created_at="2026-08-01T18:00:00Z",
        )
        brief.references[0].metadata["data_url"] = "data:image/png;base64,aW1hZ2U="

        safe = prompt_safe_design_brief(brief)

        self.assertNotIn("data_url", safe.references[0].metadata)
        self.assertTrue(safe.references[0].metadata["inline_data_supplied"])
        self.assertEqual("data:image/png;base64,aW1hZ2U=", brief.references[0].metadata["data_url"])

    def test_unsupported_schema_version_has_a_stable_structured_error(self) -> None:
        payload = brief_payload()
        payload["schema_version"] = "2.0"

        with self.assertRaises(ValidationError) as raised:
            DesignBriefCreate.model_validate(payload)

        error = raised.exception.errors()[0]
        self.assertEqual("unsupported_design_brief_schema_version", error["type"])
        self.assertEqual(["1.0"], error["ctx"]["supported_versions"])


class DesignBriefPersistenceTests(unittest.TestCase):
    def test_updates_append_traceable_versions_without_overwriting_history(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            first = database.create_design_brief_version(
                project_id,
                "user_design_brief",
                DesignBriefCreate.model_validate(brief_payload(summary="First summary")),
            )
            second = database.create_design_brief_version(
                project_id,
                "user_design_brief",
                DesignBriefCreate.model_validate(brief_payload(summary="Revised summary")),
            )
            versions = database.list_design_brief_versions(project_id, "user_design_brief")
            restored_first = database.get_design_brief_version(project_id, "user_design_brief", 1)
            latest = database.get_latest_design_brief(project_id, "user_design_brief")

        self.assertEqual(first.design_brief_id, second.design_brief_id)
        self.assertEqual(1, first.brief_version)
        self.assertIsNone(first.previous_version)
        self.assertEqual(2, second.brief_version)
        self.assertEqual(1, second.previous_version)
        self.assertEqual([1, 2], [brief.brief_version for brief in versions])
        self.assertEqual("First summary", restored_first.summary)
        self.assertEqual("Revised summary", latest.summary)
        self.assertEqual(project_id, str(latest.project_id))
        self.assertEqual("chat-design-1", latest.conversation_id)

    def test_first_writer_claim_prevents_cross_user_access(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            database.create_design_brief_version(
                project_id,
                "user_one",
                DesignBriefCreate.model_validate(brief_payload()),
            )
            with self.assertRaises(database.DesignBriefAccessError):
                database.create_design_brief_version(
                    project_id,
                    "user_two",
                    DesignBriefCreate.model_validate(brief_payload()),
                )
            self.assertEqual([], database.list_design_brief_versions(project_id, "user_two"))
            with self.assertRaises(database.DesignBriefNotFoundError):
                database.get_latest_design_brief(project_id, "user_two")

    def test_project_creation_respects_brief_owner_and_purge_removes_brief(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository(), patch("forma_core.database.invalidate_project_lists"):
            database.create_design_brief_version(
                project_id,
                "user_one",
                DesignBriefCreate.model_validate(brief_payload()),
            )
            with self.assertRaises(database.DesignBriefAccessError):
                database.save_generated_project(
                    project_id=project_id,
                    title="Wrong owner",
                    prompt="Should not persist",
                    hardware_ir={},
                    created_at="2026-08-01T18:00:00Z",
                    owner_user_id="user_two",
                )
            database.save_generated_project(
                project_id=project_id,
                title="Owned project",
                prompt="Persist for the brief owner",
                hardware_ir={},
                created_at="2026-08-01T18:00:00Z",
                owner_user_id="user_one",
            )
            self.assertTrue(database.hard_purge_generated_project(project_id, "user_one"))
            with self.assertRaises(database.DesignBriefNotFoundError):
                database.get_latest_design_brief(project_id, "user_one")


class DesignBriefApiTests(unittest.TestCase):
    def test_routes_require_authenticated_user_context(self) -> None:
        routes = [route for route in router.routes if isinstance(route, APIRoute)]
        self.assertEqual(4, len(routes))
        for route in routes:
            dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(require_user_context, dependency_calls, route.path)

    def test_api_appends_lists_and_retrieves_exact_versions(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user_context] = lambda: USER_CONTEXT
        client = TestClient(app)
        project_id = str(uuid.uuid4())

        with sqlite_repository():
            first = client.post(f"/projects/{project_id}/design-briefs", json=brief_payload(summary="First"))
            second = client.post(f"/projects/{project_id}/design-briefs", json=brief_payload(summary="Second"))
            exact = client.get(f"/projects/{project_id}/design-briefs/1")
            latest = client.get(f"/projects/{project_id}/design-briefs/latest")
            listed = client.get(f"/projects/{project_id}/design-briefs")

        self.assertEqual(201, first.status_code)
        self.assertEqual(201, second.status_code)
        self.assertEqual("First", exact.json()["summary"])
        self.assertEqual("Second", latest.json()["summary"])
        self.assertEqual([1, 2], [item["brief_version"] for item in listed.json()["versions"]])

    def test_api_rejects_unsupported_versions_structurally(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user_context] = lambda: USER_CONTEXT
        client = TestClient(app)
        payload = brief_payload()
        payload["schema_version"] = "9.0"

        response = client.post(f"/projects/{uuid.uuid4()}/design-briefs", json=payload)

        self.assertEqual(422, response.status_code)
        error = response.json()["detail"][0]
        self.assertEqual("unsupported_design_brief_schema_version", error["type"])
        self.assertEqual(["1.0"], error["ctx"]["supported_versions"])

    def test_api_requires_an_explicit_schema_version(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_user_context] = lambda: USER_CONTEXT
        client = TestClient(app)
        payload = brief_payload()
        del payload["schema_version"]

        response = client.post(f"/projects/{uuid.uuid4()}/design-briefs", json=payload)

        self.assertEqual(422, response.status_code)
        error = response.json()["detail"][0]
        self.assertEqual("missing", error["type"])
        self.assertEqual("schema_version", error["loc"][-1])


if __name__ == "__main__":
    unittest.main()
