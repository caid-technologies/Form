from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.routing import APIRoute

from apps.api import main
from apps.api.auth import UserContext, require_user_context


def _user_context(owner_user_id: str) -> UserContext:
    return UserContext(
        provider="clerk",
        subject=owner_user_id,
        owner_user_id=owner_user_id,
        is_authenticated=True,
        is_admin=False,
        claims={"sub": owner_user_id},
    )


def _anonymous_context() -> UserContext:
    return UserContext(
        provider="clerk",
        subject=None,
        owner_user_id=None,
        is_authenticated=False,
        is_admin=False,
        claims={},
    )


def _project(project_id: str, *, owner_user_id: str = "owner-a", visibility: str = "public") -> SimpleNamespace:
    return SimpleNamespace(
        project_id=project_id,
        chat_id=f"chat-{project_id}",
        owner_user_id=owner_user_id,
        visibility=visibility,
        title="Desk lamp",
        prompt="Build a desk lamp.",
        created_at="2026-08-18T12:00:00Z",
        hardware_ir={"components": [{"ref_des": "D1"}], "assembly_metadata": {"project_id": project_id}},
    )


class ProjectEngagementApiTests(unittest.TestCase):
    def test_save_and_remix_routes_require_authenticated_user_context(self) -> None:
        routes = [
            route
            for route in main.app.routes
            if isinstance(route, APIRoute) and route.path in {"/projects/{project_id}/save", "/projects/{project_id}/remix"}
        ]
        self.assertEqual(3, len(routes))
        for route in routes:
            dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(require_user_context, dependency_calls, route.path)

    def test_save_endpoint_persists_for_signed_in_reader(self) -> None:
        project = _project("public-project")
        with patch.object(main, "_resolve_project_reader", return_value=project), patch.object(
            main,
            "save_project_for_user",
            return_value={"saved": True, "save_count": 4, "remix_count": 1},
        ) as save_project:
            response = main.save_project_endpoint("public-project", _user_context("user-b"))

        save_project.assert_called_once_with("public-project", "user-b")
        self.assertTrue(response["saved"])
        self.assertEqual(4, response["save_count"])

    def test_anonymous_user_cannot_save_or_remix(self) -> None:
        project = _project("public-project")
        with patch.object(main, "_resolve_project_reader", return_value=project):
            with self.assertRaises(HTTPException) as raised_save:
                main.save_project_endpoint("public-project", _anonymous_context())
            with self.assertRaises(HTTPException) as raised_remix:
                main.remix_project_endpoint("public-project", _anonymous_context())

        self.assertEqual(401, raised_save.exception.status_code)
        self.assertEqual(401, raised_remix.exception.status_code)

    def test_remix_endpoint_returns_the_new_owned_project(self) -> None:
        source = _project("source-project")
        remixed = _project("remix-project", owner_user_id="user-b")
        with patch.object(main, "_resolve_project_reader", return_value=source), patch.object(
            main, "remix_generated_project", return_value=remixed
        ), patch.object(
            main,
            "project_engagement_for_ids",
            return_value={"source-project": {"save_count": 2, "remix_count": 3, "saved": False}},
        ):
            response = main.remix_project_endpoint("source-project", _user_context("user-b"))

        self.assertEqual("remix-project", response["project_id"])
        self.assertEqual("source-project", response["source_project_id"])
        self.assertEqual(3, response["remix_count"])
        self.assertTrue(response["can_chat"])

    def test_gallery_summary_includes_save_and_remix_counts(self) -> None:
        project = _project("public-project")
        with patch.object(main, "creator_display_name", return_value="test-user"), patch.object(
            main, "hydrate_image_storage_metadata", side_effect=lambda metadata, _project_id: metadata
        ):
            summary = main._project_summary_response(project, current_user_id=None)
        with patch.object(
            main,
            "project_engagement_for_ids",
            return_value={"public-project": {"save_count": 8, "remix_count": 2, "saved": True}},
        ):
            enriched = main._with_project_engagement([summary], "user-b")

        self.assertNotIn("star_count", summary)
        self.assertEqual(8, enriched[0]["save_count"])
        self.assertEqual(2, enriched[0]["remix_count"])
        self.assertTrue(enriched[0]["saved"])


if __name__ == "__main__":
    unittest.main()
