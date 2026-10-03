from __future__ import annotations

import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from apps.api import main
from apps.api.auth import UserContext
from forma_core import database
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workspaces.projects.design_lifecycle import (
    VisualApprovalStatus,
    bootstrap_design_lifecycle,
    load_design_lifecycle,
    register_system_render,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, SystemArchitecture, SystemNode


OWNER = "progressive-review-user"
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
            source="progressive visual review test",
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


def progressive_project() -> HardwareIntermediateRepresentation:
    ir = HardwareIntermediateRepresentation(
        system_architecture=SystemArchitecture(
            summary="Small controller",
            root=SystemNode(
                system_id="product",
                name="Controller",
                domain="product",
                purpose="House and operate a small controller.",
            ),
        ),
        assembly_metadata={
            "generation_mode": "progressive",
            "visual_approval_policy": "require_approval",
        },
    )
    bootstrap_design_lifecycle(ir, policy="require_approval")
    register_system_render(
        ir,
        source_fingerprint="sha256:system-visual",
        uri="https://images.example.test/system.png",
    )
    return ir


def persist_project(project_id: str) -> None:
    database.persist_chat_project_revision(
        project_id,
        OWNER,
        progressive_project(),
        source_job_id="initial-progressive-review",
        prompt="Build a small controller.",
        chat_id="chat-progressive-review",
    )


def test_approve_persists_without_starting_cad() -> None:
    project_id = str(uuid.uuid4())
    with sqlite_repository():
        persist_project(project_id)
        with (
            patch.object(main, "require_hosted_chat_enabled"),
            patch.object(main, "ensure_project_action_allowed"),
            patch.object(main, "ensure_native_cad_model", return_value=True) as ensure_cad,
        ):
            response = main.project_visual_decision_endpoint(
                project_id,
                main.ProgressiveVisualDecisionRequest(decision="approve"),
                USER,
            )

        ensure_cad.assert_not_called()
        assert response["can_chat"] is True
        assert response["cad_generated"] is False
        assert response["visual_approval_status"] == "approved"

        persisted = database.resolve_project_for_read(project_id, OWNER).revision
        assert persisted is not None
        assert load_design_lifecycle(persisted.state).visual_gate.status == VisualApprovalStatus.APPROVED


def test_revise_requires_feedback_and_persists_rejection() -> None:
    project_id = str(uuid.uuid4())
    with sqlite_repository():
        persist_project(project_id)
        with (
            patch.object(main, "require_hosted_chat_enabled"),
            patch.object(main, "ensure_project_action_allowed"),
        ):
            try:
                main.project_visual_decision_endpoint(
                    project_id,
                    main.ProgressiveVisualDecisionRequest(decision="revise"),
                    USER,
                )
            except main.HTTPException as exc:
                assert exc.status_code == 400
            else:
                raise AssertionError("revise without feedback should be rejected")

            response = main.project_visual_decision_endpoint(
                project_id,
                main.ProgressiveVisualDecisionRequest(
                    decision="revise",
                    feedback="Move the connector to the rear panel.",
                ),
                USER,
            )

        assert response["visual_approval_status"] == "rejected"
        persisted = database.resolve_project_for_read(project_id, OWNER).revision
        assert persisted is not None
        gate = load_design_lifecycle(persisted.state).visual_gate
        assert gate.status == VisualApprovalStatus.REJECTED
        assert gate.feedback == "Move the connector to the rear panel."


def test_continue_to_cad_is_the_explicit_expensive_step() -> None:
    project_id = str(uuid.uuid4())
    with sqlite_repository():
        persist_project(project_id)
        with (
            patch.object(main, "require_hosted_chat_enabled"),
            patch.object(main, "ensure_project_action_allowed"),
            patch.object(main, "ensure_native_cad_model", return_value=True) as ensure_cad,
        ):
            response = main.project_visual_decision_endpoint(
                project_id,
                main.ProgressiveVisualDecisionRequest(decision="continue_to_cad"),
                USER,
            )

        ensure_cad.assert_called_once()
        assert response["cad_generated"] is True
        assert response["visual_approval_status"] == "approved"
        assert response["project_ir"]["assembly_metadata"]["can_chat"] is True
