from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.opencode_api import router
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.opencode.models import OpenCodeOperation
from forma_core.opencode.store import OpenCodeStore
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.outcomes import evaluate_design_outcome
from forma_core.workspaces.projects.state import ProjectRevision, ProjectArtifact


def wired_project() -> HardwareIntermediateRepresentation:
    return HardwareIntermediateRepresentation.model_validate({
        "components": [{"ref_des": ref, "part_number": ref, "name": ref,
                        "category": "Module", "rationale": "USB supply",
                        "pins": [{"pin_id": "VBUS", "name": "USB power", "pin_type": "Power", "voltage": 5}]}
                       for ref in ("U1", "J1")],
        "nets": [{"net_id": "USB", "name": "USB power", "net_type": "Power",
                  "pins": [{"ref_des": ref, "pin_id": "VBUS"} for ref in ("U1", "J1")]}],
    })


class SavedOutcomeTests(unittest.TestCase):
    def test_readiness_requires_more_than_a_component_or_self_declared_validity(self) -> None:
        draft = HardwareIntermediateRepresentation(assembly_metadata={"project_readiness": "complete"})
        self.assertEqual("draft", evaluate_design_outcome(draft).project_readiness)
        project = wired_project()
        self.assertEqual("complete", evaluate_design_outcome(project).project_readiness)
        project.nets = []
        project.is_valid = True
        outcome = evaluate_design_outcome(project)
        self.assertEqual("partial", outcome.project_readiness)
        self.assertFalse(outcome.is_valid)

    def test_completion_verifies_saved_draft_wired_invalid_and_missing_results(self) -> None:
        invalid = wired_project()
        invalid.nets = []
        for project, expected in ((HardwareIntermediateRepresentation(), "draft"), (wired_project(), "complete"), (invalid, "partial"), (None, None)):
            with self.subTest(expected=expected), patch.dict(os.environ, {"FORMA_USER_SECRETS_KEY": "isolated-test-key"}):
                store = OpenCodeStore(":memory:")
                self.addCleanup(store.close)
                project_id = uuid4()
                session = store.create_session(session_id="session", connector_id="mini", owner_user_id="owner", project_id=str(project_id))
                store.create_command(command_id="command", session=session, operation=OpenCodeOperation.PROJECT_MESSAGE, idempotency_key="key", message="build")
                claimed = store.claim_next(connector_id="mini", session_id="session")
                revision = ProjectRevision(
                    state=project, components=project.components, revision_id=uuid4(), project_id=project_id,
                    owner_user_id="owner", revision=3, design_brief_id=uuid4(), design_brief_version=1,
                    source_job_id="test", artifacts=[ProjectArtifact(artifact_id="artifact", kind="schematic", uri="private://not-for-browser")],
                ) if project is not None else None
                cap = ConnectorCapability("mini", "session", str(project_id), "owner", 2_000_000_000, "nonce", frozenset({"complete"}))
                app = FastAPI()
                app.include_router(router)
                with patch("apps.api.opencode_api.OPENCODE_STORE", store), \
                     patch("apps.api.opencode_api._connector_command", return_value=store.get_command("command")), \
                     patch("apps.api.opencode_api._connector_capability", return_value=cap), \
                     patch("apps.api.opencode_api._scoped_connector_session", return_value=session), \
                     patch("apps.api.opencode_api.get_latest_project_revision", return_value=revision) as latest, \
                     TestClient(app) as client:
                    response = client.post("/opencode/connector/commands/command/complete", json={"lease_token": claimed.lease_token, "status": "succeeded"})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual("succeeded", response.json()["status"])
                    latest.assert_called_once_with(str(project_id), "owner")
                event = store.list_events("session", 0, 10)[0]
                self.assertEqual("completed", event.kind)
                self.assertNotIn("private://", event.model_dump_json())
                if revision:
                    self.assertEqual(str(revision.revision_id), event.revision_id)
                    self.assertEqual(expected, event.design_outcome.project_readiness)
                    self.assertEqual(("artifact",), event.artifact_ids)
                    self.assertEqual(expected != "partial", event.validation.is_valid)
                else:
                    self.assertIsNone(event.revision_id)
                    self.assertIsNone(event.design_outcome)
                    self.assertIsNone(event.validation)
                    self.assertEqual((), event.artifact_ids)
