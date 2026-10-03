from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from uuid import uuid4

from forma_core.workspaces.design_briefs import DesignBrief, DESIGN_BRIEF_SCHEMA_VERSION
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ProjectOverview
from forma_core.workspaces.projects.resolver import (
    ProjectReadNotFoundError,
    ProjectReadResolver,
)
from forma_core.workspaces.projects.state import ProjectRevision, ProjectStateError


PROJECT_ID = "11111111-1111-4111-8111-111111111111"


def _ir() -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation(
        overview=ProjectOverview(
            title="Resolver project",
            description="A resolver test project.",
            difficulty="Beginner",
            category="IoT",
        ),
        assembly_metadata={"project_id": PROJECT_ID, "product_image_url": "https://example.test/image.png"},
    )


def _brief() -> DesignBrief:
    return DesignBrief(
        schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
        conversation_id="resolver-chat",
        intent="Build a resolver test project",
        summary="Build a resolver test project.",
        design_brief_id=uuid4(),
        project_id=PROJECT_ID,
        brief_version=1,
        created_at=datetime.now(timezone.utc),
    )


def _revision(brief: DesignBrief) -> ProjectRevision:
    return ProjectRevision(
        revision_id=uuid4(),
        project_id=PROJECT_ID,
        owner_user_id="owner-a",
        revision=3,
        parent_revision=2,
        design_brief_id=brief.design_brief_id,
        design_brief_version=brief.brief_version,
        source_job_id="iteration-3",
        state=_ir(),
        created_at=datetime.now(timezone.utc),
    )


class ProjectReadResolverTests(unittest.TestCase):
    def test_hosted_projection_resolves_and_logs_legacy_fallback(self) -> None:
        project = SimpleNamespace(
            project_id=PROJECT_ID,
            owner_user_id="owner-a",
            creation_channel="hosted",
            chat_id="hosted-chat",
            title="Hosted project",
            prompt="Build a hosted project.",
            created_at="2026-08-01T12:00:00Z",
            updated_at="2026-08-01T12:00:00Z",
            visibility="public",
            status="active",
            hardware_ir=_ir().model_dump(mode="json"),
        )
        repository = Mock()
        repository.get_generated_project.return_value = project
        resolver = ProjectReadResolver(repository)
        resolver._state.get_latest = Mock(side_effect=ProjectStateError("project_revision_not_found", "missing"))

        with self.assertLogs("forma_core.workspaces.projects.resolver", level="INFO") as logs:
            resolved = resolver.resolve(PROJECT_ID)

        self.assertEqual("generated", resolved.source)
        self.assertTrue(resolved.legacy_fallback)
        self.assertEqual("https://example.test/image.png", resolved.image_metadata["product_image_url"])
        self.assertFalse(resolved.can_chat)
        self.assertIn("project_read_resolver_legacy_fallback", "\n".join(logs.output))

    def test_canonical_only_project_resolves_when_projection_is_missing(self) -> None:
        brief = _brief()
        revision = _revision(brief)
        repository = Mock()
        repository.get_generated_project.return_value = None
        repository.get_latest_design_brief.return_value = SimpleNamespace(payload_json=brief.model_dump(mode="json"))
        repository.get_cli_project_revision.return_value = None
        resolver = ProjectReadResolver(repository)
        resolver._state.get_latest = Mock(return_value=revision)

        resolved = resolver.resolve(PROJECT_ID, "owner-a")

        self.assertEqual("canonical", resolved.source)
        self.assertEqual(3, resolved.current_revision)
        self.assertEqual("resolver-chat", resolved.chat_id)
        self.assertTrue(resolved.can_chat)
        self.assertFalse(resolved.legacy_fallback)

    def test_canonical_revision_resolves_without_design_brief(self) -> None:
        revision = _revision(_brief())
        repository = Mock()
        repository.get_generated_project.return_value = None
        repository.get_latest_design_brief.return_value = None
        repository.get_cli_project_revision.return_value = None
        resolver = ProjectReadResolver(repository)
        resolver._state.get_latest = Mock(return_value=revision)

        resolved = resolver.resolve(PROJECT_ID, "owner-a")

        self.assertEqual("canonical", resolved.source)
        self.assertEqual("Resolver project", resolved.title)
        self.assertEqual("A resolver test project.", resolved.prompt)
        self.assertIsNone(resolved.chat_id)
        self.assertFalse(resolved.can_chat)

    def test_cli_project_resolves_when_generated_projection_is_missing(self) -> None:
        repository = Mock()
        repository.get_generated_project.return_value = None
        repository.get_cli_project_revision.return_value = SimpleNamespace(
            revision_id="cli-revision-1",
            revision=1,
            created_at="2026-08-02T12:00:00Z",
            manifest_json={
                "format": "forma-project",
                "version": 1,
                "project_id": PROJECT_ID,
                "title": "CLI project",
                "prompt": "Build a CLI project.",
                "project_ir": _ir().model_dump(mode="json"),
            },
        )
        resolver = ProjectReadResolver(repository)
        resolver._state.get_latest = Mock(side_effect=ProjectStateError("project_revision_not_found", "missing"))

        resolved = resolver.resolve(PROJECT_ID, "owner-a")

        self.assertEqual("cli", resolved.source)
        self.assertEqual("cli", resolved.creation_channel.value)
        self.assertEqual(1, resolved.current_revision)
        self.assertFalse(resolved.can_chat)

    def test_cli_project_defaults_to_public_visibility(self) -> None:
        repository = Mock()
        repository.get_project_identity.return_value = {
            "project_id": PROJECT_ID,
            "owner_user_id": "owner-a",
            "creation_channel": "cli",
            "visibility": "public",
            "status": "active",
        }
        repository.get_generated_project.return_value = None
        repository.get_cli_project_revision.return_value = SimpleNamespace(
            revision_id="cli-revision-public",
            revision=1,
            created_at="2026-08-02T12:00:00Z",
            manifest_json={
                "format": "forma-project",
                "version": 1,
                "project_id": PROJECT_ID,
                "title": "Public CLI project",
                "prompt": "Build a public CLI project.",
                "project_ir": _ir().model_dump(mode="json"),
            },
        )

        resolved = ProjectReadResolver(repository).resolve(PROJECT_ID)

        self.assertEqual("public", resolved.visibility)

    def test_private_and_deleted_projects_are_hidden_without_owner_access(self) -> None:
        for status, include_deleted in (("active", False), ("deletion_pending", True)):
            project = SimpleNamespace(
                project_id=PROJECT_ID,
                owner_user_id="owner-a",
                visibility="private",
                status=status,
                hardware_ir={},
            )
            repository = Mock()
            repository.get_generated_project.return_value = project
            resolver = ProjectReadResolver(repository)

            with self.assertRaises(ProjectReadNotFoundError):
                resolver.resolve(PROJECT_ID, "owner-b", include_deleted=include_deleted)

    def test_canonical_lifecycle_hides_deleted_projection_and_allows_owner_recovery_read(self) -> None:
        project = SimpleNamespace(
            project_id=PROJECT_ID,
            owner_user_id="owner-a",
            creation_channel="hosted",
            visibility="public",
            status="active",
            hardware_ir=_ir().model_dump(mode="json"),
        )
        repository = Mock()
        repository.get_project_identity.return_value = {
            "project_id": PROJECT_ID,
            "owner_user_id": "owner-a",
            "visibility": "public",
            "status": "deletion_pending",
        }
        repository.get_generated_project.return_value = project
        resolver = ProjectReadResolver(repository)

        with self.assertRaises(ProjectReadNotFoundError):
            resolver.resolve(PROJECT_ID, "owner-a")

        resolved = resolver.resolve(PROJECT_ID, "owner-a", include_deleted=True)

        self.assertEqual("deletion_pending", resolved.status)
        self.assertEqual("owner-a", resolved.owner_user_id)

    def test_owner_can_resolve_deleted_lifecycle_state_when_requested(self) -> None:
        project = SimpleNamespace(
            project_id=PROJECT_ID,
            owner_user_id="owner-a",
            visibility="private",
            status="deletion_pending",
            hardware_ir={},
        )
        repository = Mock()
        repository.get_generated_project.return_value = project

        resolved = ProjectReadResolver(repository).resolve(PROJECT_ID, "owner-a", include_deleted=True)

        self.assertEqual("deletion_pending", resolved.status)
        self.assertFalse(resolved.can_chat)


if __name__ == "__main__":
    unittest.main()
