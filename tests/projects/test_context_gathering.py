from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Iterator
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.api import a2a, main
from apps.api.a2a import A2AMessage
from apps.api.auth import UserContext, require_user_context
from apps.api.context_builds import ContextBuildDispatcher, context_build_dispatcher
from apps.api.context_gathering_api import context_gathering_agent, router
from apps.api.worker_plans_api import router as worker_plans_router
from forma_core.agents.context_gathering import ContextGatheringAgent
from forma_core import database
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workers import WorkerPlanStatus
from forma_core.workspaces.projects.models import GenerateProjectRequest
from forma_core.workspaces.context import ContextBuildExecution, ContextTurnDecision
from forma_core.workspaces.context.agent import ContextBriefUpdater
from forma_core.workspaces.context.models import ContextAttachment, ContextGatheringRequest
from forma_core.workspaces.context.pdf import PdfContextResult
from forma_core.workspaces.workflow import ProjectWorkflowState, WorkflowActorType, WorkflowStateError
from forma_core.vertex_auth import (
    bind_vertex_oidc_token,
    current_vertex_oidc_token,
    reset_vertex_oidc_token,
)


OWNER = "context-user"
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
            source="context gathering test",
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


class ContextBriefImageReferenceTests(unittest.TestCase):
    def test_uploaded_image_keeps_inline_data_on_the_design_brief(self) -> None:
        brief, *_ = ContextBriefUpdater().update(ContextGatheringRequest(
            conversation_id="chat-reference",
            text="Build a lamp from this enclosure.",
            attachments=[
                ContextAttachment(
                    attachment_id="clipboard-image",
                    kind="image",
                    name="hardware-reference.png",
                    media_type="image/png",
                    data_url="data:image/png;base64,aW1hZ2U=",
                    source="clipboard",
                )
            ],
        ))

        self.assertEqual(1, len(brief.references))
        self.assertEqual("uploaded_image", brief.references[0].kind)
        self.assertEqual("data:image/png;base64,aW1hZ2U=", brief.references[0].metadata["data_url"])
        self.assertTrue(brief.references[0].metadata["inline_data_supplied"])


class ContextGatheringIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.include_router(worker_plans_router)
        app.dependency_overrides[require_user_context] = lambda: USER
        app.dependency_overrides[context_gathering_agent] = lambda: ContextGatheringAgent()
        app.dependency_overrides[context_build_dispatcher] = lambda: None
        self.app = app
        self.client = TestClient(app)

    def test_chat_discovery_does_not_create_or_mutate_a_design_brief(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-only"
        with sqlite_repository():
            greeting = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "hi"},
            )
            discovery = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "what can I build?"},
            )
            versions = database.list_design_brief_versions(project_id, OWNER)

        self.assertEqual(201, greeting.status_code, greeting.text)
        self.assertEqual("chat", greeting.json()["turn_kind"])
        self.assertIsNone(greeting.json()["workflow"])
        self.assertIsNone(greeting.json()["design_brief"])
        self.assertEqual("chat", discovery.json()["turn_kind"])
        self.assertEqual([], versions)

    def test_proceed_turn_advances_once_and_never_hits_context_writer(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-proceed"
        with sqlite_repository():
            context = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a handheld environmental monitor."},
            )
            first = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "go ahead"},
            )
            replay = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "go ahead"},
            )

        self.assertEqual(201, context.status_code, context.text)
        self.assertEqual("context", context.json()["turn_kind"])
        self.assertEqual(201, first.status_code, first.text)
        self.assertEqual("proceed", first.json()["turn_kind"])
        self.assertEqual("ready_to_build", first.json()["workflow"]["state"])
        self.assertEqual(201, replay.status_code, replay.text)
        self.assertEqual("proceed", replay.json()["turn_kind"])
        self.assertEqual("ready_to_build", replay.json()["workflow"]["state"])

    def test_first_turn_build_intent_bootstraps_the_brief_and_starts_the_build(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-first-turn-build"
        prompt = (
            "Build me a chip that supports a Mamba-like latent-space model as an orchestrator "
            "for an LLM and reinforcement-learning model. Start with a scalable compute tile "
            "for a 7B to 30B parameter reference workload with HBM-attached tensor compute."
        )

        class FirstTurnBuildAgent(ContextGatheringAgent):
            def route_turn(self, *_args, **_kwargs):
                return ContextTurnDecision(
                    turn_kind="proceed",
                    tool_name="build_project",
                    assistant_message="I’ll start the design.",
                )

        self.app.dependency_overrides[context_gathering_agent] = lambda: FirstTurnBuildAgent()
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ):
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": prompt},
            )
            brief = database.get_latest_design_brief(project_id, OWNER)

        self.assertEqual(201, response.status_code, response.text)
        body = response.json()
        self.assertEqual("building", body["workflow"]["state"])
        self.assertIsNotNone(body["build_execution"])
        self.assertIn("started the design", body["assistant_message"])
        self.assertEqual([], body["suggestions"])
        persisted_requirements = " ".join(brief.requirements)
        self.assertIn("Mamba-like latent-space model", persisted_requirements)
        self.assertIn("7B to 30B parameter reference workload", persisted_requirements)
        self.assertNotIn("Tell me what you want to build first", body["assistant_message"])

    def test_first_turn_build_control_without_project_context_does_not_start(self) -> None:
        project_id = str(uuid.uuid4())

        class FirstTurnBuildAgent(ContextGatheringAgent):
            def route_turn(self, *_args, **_kwargs):
                return ContextTurnDecision(
                    turn_kind="proceed",
                    tool_name="build_project",
                    assistant_message="I’ll start the design.",
                )

        self.app.dependency_overrides[context_gathering_agent] = lambda: FirstTurnBuildAgent()
        with sqlite_repository():
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": "conversation-no-context", "text": "start"},
            )

        self.assertEqual(201, response.status_code, response.text)
        body = response.json()
        self.assertIsNone(body["workflow"])
        self.assertIsNone(body["design_brief"])
        self.assertIsNone(body["build_execution"])
        self.assertEqual("clarification", body["turn_kind"])
        self.assertEqual("ask_question", body["tool_name"])
        self.assertIn("Tell me what you want to build first", body["assistant_message"])

    def test_stuck_chat_recovers_prior_project_context_on_the_next_build_command(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-recover-build"
        original_prompt = (
            "Build a scalable compute tile with HBM-attached tensor and sequence compute, "
            "FP8 and INT8 inference, and a tile-to-tile interconnect."
        )

        class BuildAgent(ContextGatheringAgent):
            def route_turn(self, *_args, **_kwargs):
                return ContextTurnDecision(
                    turn_kind="proceed",
                    tool_name="build_project",
                    assistant_message="I’ll start the design.",
                )

        self.app.dependency_overrides[context_gathering_agent] = lambda: BuildAgent()
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ):
            database.upsert_project_chat(
                chat_id=conversation_id,
                owner_user_id=OWNER,
                title="Scalable compute tile",
                messages=[
                    {"role": "user", "content": original_prompt},
                    {
                        "role": "assistant",
                        "content": "Tell me what you want to build first, and I’ll help shape it and start the design.",
                    },
                ],
                created_at="2026-08-10T09:09:00Z",
                updated_at="2026-08-10T09:09:00Z",
            )
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "start"},
            )
            brief = database.get_latest_design_brief(project_id, OWNER)

        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual("building", response.json()["workflow"]["state"])
        self.assertIsNotNone(response.json()["build_execution"])
        self.assertIn("HBM-attached tensor and sequence compute", " ".join(brief.requirements))

    def test_proceed_dispatches_a_real_build_stage_when_a_dispatcher_is_available(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-build-dispatch"
        build_id = uuid.uuid4()

        class RecordingDispatcher:
            calls = 0

            def start(self, current_project_id, owner, current_conversation_id):
                self.calls += 1
                outcome = database.transition_project_workflow(
                    current_project_id,
                    owner,
                    ProjectWorkflowState.BUILDING,
                    actor_type=WorkflowActorType.USER,
                    actor_id=owner,
                    reason="Test dispatcher started generation.",
                    idempotency_key=f"test-build:{current_conversation_id}",
                )
                return ContextBuildExecution(
                    build_id=build_id,
                    plan_id="build-plan-test",
                    job_id="generation-test",
                    status="planned",
                ), outcome.workflow

        dispatcher = RecordingDispatcher()
        self.app.dependency_overrides[context_build_dispatcher] = lambda: dispatcher
        with sqlite_repository():
            context = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a handheld environmental monitor."},
            )
            started = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "do it now"},
            )

        self.assertEqual(201, context.status_code, context.text)
        self.assertEqual(201, started.status_code, started.text)
        self.assertEqual("building", started.json()["workflow"]["state"])
        self.assertEqual("build-plan-test", started.json()["build_execution"]["plan_id"])
        self.assertIn("started the design", started.json()["assistant_message"])
        self.assertEqual(1, dispatcher.calls)

    def test_production_dispatcher_freezes_build_creates_plan_and_schedules_execution(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-production-dispatch"
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ) as launch_plan:
            context = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Environmental monitor with sensor feedback, display, and battery power.",
                },
            )
            started = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "start"},
            )
            execution = started.json()["build_execution"]
            plan = database.get_project_generation_plan(execution["plan_id"], OWNER)
            frozen = database.get_latest_project_build(project_id, OWNER)

        self.assertEqual(201, context.status_code, context.text)
        self.assertTrue(context.json()["questions"])
        self.assertEqual(201, started.status_code, started.text)
        self.assertEqual("building", started.json()["workflow"]["state"])
        self.assertNotIn("critical choice", started.json()["assistant_message"])
        self.assertEqual("planned", execution["status"])
        self.assertEqual(str(frozen.build_id), execution["build_id"])
        self.assertEqual([execution["job_id"]], list(plan.jobs))
        launch_plan.assert_called_once_with(execution["plan_id"], OWNER)

    def test_vercel_dispatcher_leaves_plan_for_request_bound_execution(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-vercel-dispatch"
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch.dict(os.environ, {"VERCEL": "1"}), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ) as launch_plan:
            self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a relay controller."},
            )
            started = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "start"},
            )

        self.assertEqual(201, started.status_code, started.text)
        self.assertEqual("planned", started.json()["build_execution"]["status"])
        launch_plan.assert_not_called()

    def test_detached_build_worker_inherits_request_vertex_oidc_token(self) -> None:
        observed: list[str | None] = []
        completed = Event()

        async def execute_plan(*_args, **_kwargs):
            observed.append(current_vertex_oidc_token())
            completed.set()

        request_context = bind_vertex_oidc_token("runtime-vercel-token")
        try:
            with patch("apps.api.context_builds.execute_project_generation_plan", side_effect=execute_plan):
                ContextBuildDispatcher._launch("build-plan-oidc", OWNER)
                self.assertTrue(completed.wait(timeout=2), "Detached build worker did not finish.")
        finally:
            reset_vertex_oidc_token(request_context)

        self.assertEqual(["runtime-vercel-token"], observed)

    def test_worker_plan_cancel_endpoint_stops_the_durable_build(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-cancel-build"
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ):
            self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build an environmental monitor."},
            )
            started = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "start"},
            )
            execution = started.json()["build_execution"]
            stopped = self.client.post(
                f"/projects/{project_id}/build/plans/{execution['plan_id']}/cancel",
            )
            persisted = database.get_project_generation_plan(execution["plan_id"], OWNER)

        self.assertEqual(200, stopped.status_code, stopped.text)
        self.assertEqual("cancelled", stopped.json()["status"])
        self.assertEqual("cancelled", stopped.json()["jobs"][execution["job_id"]]["status"])
        self.assertEqual("cancelled", persisted.status.value)

    def test_worker_plan_execute_endpoint_keeps_generation_in_request_lifecycle(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-request-bound-build"
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ):
            self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a relay controller."},
            )
            started = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "start"},
            )
            execution = started.json()["build_execution"]
            plan = database.get_project_generation_plan(execution["plan_id"], OWNER)
            with patch.object(
                ContextBuildDispatcher,
                "execute",
                new=AsyncMock(return_value=plan),
            ) as execute_plan:
                response = self.client.post(
                    f"/projects/{project_id}/build/plans/{execution['plan_id']}/execute",
                )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(execution["plan_id"], response.json()["plan_id"])
        execute_plan.assert_awaited_once_with(execution["plan_id"], OWNER)

    def test_worker_plan_reset_endpoint_resets_owned_failed_build(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "conversation-reset-build"
        self.app.dependency_overrides.pop(context_build_dispatcher)
        with sqlite_repository(), patch(
            "apps.api.context_builds.ContextBuildDispatcher._launch",
        ):
            self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a relay controller."},
            )
            started = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "start"},
            )
            execution = started.json()["build_execution"]
            plan = database.get_project_generation_plan(execution["plan_id"], OWNER)
            reset_plan = plan.model_copy(update={"status": WorkerPlanStatus.PLANNED, "attempt": 2})
            with patch(
                "apps.api.worker_plans_api.reset_project_generation_plan",
                new=AsyncMock(return_value=reset_plan),
            ) as reset:
                response = self.client.post(
                    f"/projects/{project_id}/build/plans/{execution['plan_id']}/reset",
                )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("planned", response.json()["status"])
        self.assertEqual(2, response.json()["attempt"])
        reset.assert_awaited_once_with(execution["plan_id"], OWNER)

    def test_pdf_uri_reference_remains_a_reference_without_server_fetching(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-pdf-uri"

        with sqlite_repository(), patch(
            "apps.api.context_gathering_api.ingest_pdf_data_url",
        ) as extract:
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Use this controller datasheet as a reference.",
                    "attachments": [
                        {
                            "attachment_id": "datasheet-uri",
                            "kind": "document",
                            "name": "controller.pdf",
                            "media_type": "application/pdf",
                            "uri": "https://example.test/controller.pdf",
                            "source": "url",
                        }
                    ],
                },
            )
            brief = database.get_latest_design_brief(project_id, OWNER)

        self.assertEqual(201, response.status_code, response.text)
        extract.assert_not_called()
        reference = next(item for item in brief.references if item.reference_id == "datasheet-uri")
        self.assertEqual("https://example.test/controller.pdf", reference.uri)
        self.assertEqual("uploaded_document", reference.kind)
        self.assertNotIn("text_extracted", reference.metadata)

    def test_pdf_attachment_extracts_text_without_persisting_inline_bytes(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-pdf"
        pdf_data_url = "data:application/pdf;base64,JVBERi0xLjQK"

        with sqlite_repository(), patch(
            "apps.api.context_gathering_api.ingest_pdf_data_url",
            return_value=PdfContextResult(
                text="The enclosure must fit within 80 mm. Use M3 fasteners.",
                visual_data_url=None,
                visual_page_numbers=(),
                source_digest="datasheetdigest",
                page_count=3,
                text_truncated=False,
            ),
        ) as extract:
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Use the attached datasheet as project context.",
                    "attachments": [
                        {
                            "attachment_id": "datasheet-pdf",
                            "kind": "document",
                            "name": "datasheet.pdf",
                            "media_type": "application/pdf",
                            "data_url": pdf_data_url,
                            "source": "upload",
                        }
                    ],
                },
            )
            brief = database.get_latest_design_brief(project_id, OWNER)
            chat = database.get_project_chat(conversation_id, OWNER)

        self.assertEqual(201, response.status_code, response.text)
        extract.assert_called_once_with(pdf_data_url)
        self.assertIn("The enclosure must fit within 80 mm.", brief.requirements)
        reference = next(item for item in brief.references if item.reference_id == "datasheet-pdf")
        self.assertEqual("uploaded_document", reference.kind)
        self.assertEqual("application/pdf", reference.media_type)
        self.assertEqual("urn:forma:pdf:datasheetdigest", reference.uri)
        self.assertTrue(reference.metadata["text_extracted"])
        self.assertNotIn("data_url", reference.metadata)

        user_message = next(
            message
            for message in chat.messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        attachment = user_message["attachments"][0]
        self.assertFalse(attachment["hasInlineData"])
        self.assertTrue(attachment["textExtracted"])

    def test_pdf_attachment_adds_traceable_visual_pages_without_chat_blob(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-pdf-visual"
        pdf_data_url = "data:application/pdf;base64,JVBERi0xLjQK"
        visual_data_url = "data:image/jpeg;base64,dmlzdWFsLXBhZ2Vz"

        with sqlite_repository(), patch(
            "apps.api.context_gathering_api.ingest_pdf_data_url",
            return_value=PdfContextResult(
                text="[PDF page 2]\nUse the JST-XH connector shown in the drawing.",
                visual_data_url=visual_data_url,
                visual_page_numbers=(2, 5),
                source_digest="visualdigest",
                page_count=8,
                text_truncated=False,
            ),
        ):
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Use the attached datasheet as project context.",
                    "attachments": [
                        {
                            "attachment_id": "datasheet-pdf",
                            "kind": "document",
                            "name": "datasheet.pdf",
                            "media_type": "application/pdf",
                            "data_url": pdf_data_url,
                            "source": "upload",
                        }
                    ],
                },
            )
            brief = database.get_latest_design_brief(project_id, OWNER)
            chat = database.get_project_chat(conversation_id, OWNER)

        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual(2, len(brief.references))
        document_reference = next(
            item for item in brief.references if item.reference_id == "datasheet-pdf"
        )
        visual_reference = next(
            item for item in brief.references if item.reference_id == "datasheet-pdf-visual-pages"
        )
        self.assertEqual("uploaded_document", document_reference.kind)
        self.assertEqual([2, 5], document_reference.metadata["pdf_visual_pages"])
        self.assertEqual("uploaded_image", visual_reference.kind)
        self.assertEqual([2, 5], visual_reference.metadata["pdf_page_numbers"])
        self.assertTrue(visual_reference.metadata["derived_from_pdf"])
        self.assertEqual(visual_data_url, visual_reference.metadata["data_url"])

        user_message = next(
            message
            for message in chat.messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        self.assertNotIn("imagePreview", user_message)
        self.assertEqual(2, len(user_message["attachments"]))
        self.assertFalse(user_message["attachments"][0]["hasInlineData"])
        self.assertTrue(user_message["attachments"][1]["hasInlineData"])
        self.assertEqual([2, 5], user_message["attachments"][1]["metadata"]["pdf_page_numbers"])

    def test_text_image_and_document_append_brief_versions_without_enqueuing_jobs(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-chat-1"

        with sqlite_repository(), patch.object(a2a.JOB_STORE, "create_job") as create_job:
            first = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Build an ESP32 environmental monitor with USB-C power and wiring.",
                },
            )
            second = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "It must fit within 100 mm and include product images.",
                    "attachments": [
                        {
                            "attachment_id": "clipboard-image",
                            "kind": "image",
                            "name": "reference.png",
                            "media_type": "image/png",
                            "data_url": "data:image/png;base64,aW1hZ2U=",
                            "source": "clipboard",
                        },
                        {
                            "attachment_id": "requirements-document",
                            "kind": "document",
                            "name": "requirements.txt",
                            "media_type": "text/plain",
                            "extracted_text": "The display must remain readable outdoors.",
                        },
                    ],
                },
            )
            versions = database.list_design_brief_versions(project_id, OWNER)
            chat = database.get_project_chat(conversation_id, OWNER)

        self.assertEqual(201, first.status_code, first.text)
        self.assertEqual("gathering_context", first.json()["workflow"]["state"])
        self.assertTrue(first.json()["questions"])
        self.assertTrue(first.json()["suggestions"])
        self.assertNotIn("Other", first.json()["suggestions"])
        self.assertEqual(201, second.status_code, second.text)
        self.assertEqual(2, second.json()["design_brief"]["brief_version"])
        self.assertEqual([1, 2], [brief.brief_version for brief in versions])
        self.assertEqual(2, len(versions[-1].references))
        image_reference = next(item for item in versions[-1].references if item.kind == "uploaded_image")
        self.assertEqual("data:image/png;base64,aW1hZ2U=", image_reference.metadata["data_url"])
        self.assertIn("product images", versions[-1].requested_outputs)
        self.assertIn("The display must remain readable outdoors.", versions[-1].requirements)
        self.assertEqual(4, len(chat.messages))
        image_messages = [
            message for message in chat.messages
            if isinstance(message, dict) and message.get("imagePreview")
        ]
        self.assertEqual(["data:image/png;base64,aW1hZ2U="], [message["imagePreview"] for message in image_messages])
        create_job.assert_not_called()

    def test_agent_suggestions_are_returned_and_persisted_with_the_assistant_turn(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-chat-suggestions"

        class SuggestedAnswerAgent(ContextGatheringAgent):
            def route_turn(self, *_args, **_kwargs):
                return ContextTurnDecision(
                    turn_kind="context",
                    tool_name="ask_question",
                    save_context=True,
                    assistant_message="Which deployment environment should I design for?",
                    suggestions=["3-season", "4-season", "Other"],
                )

        self.app.dependency_overrides[context_gathering_agent] = lambda: SuggestedAnswerAgent()
        with sqlite_repository():
            response = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a one-person tent."},
            )
            chat = database.get_project_chat(conversation_id, OWNER)

        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual(["3-season", "4-season"], response.json()["suggestions"])
        self.assertEqual(["3-season", "4-season"], chat.messages[-1]["suggestions"])

    def test_generation_and_mutating_tools_are_blocked_during_gathering(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository():
            database.initialize_project_workflow(project_id, OWNER)

            for action in ("forma.generate_project", "fabricator.plan", "opencad.mutate"):
                with self.subTest(action=action), self.assertRaises(WorkflowStateError) as raised:
                    database.ensure_project_action_allowed(project_id, OWNER, action, require_workflow=True)
                self.assertEqual("tool_execution_blocked_while_gathering_context", raised.exception.code)

    def test_generate_endpoint_rejects_before_worker_job_is_created(self) -> None:
        project_id = str(uuid.uuid4())
        with sqlite_repository(), patch.object(main.JOB_STORE, "create_job") as create_job:
            database.initialize_project_workflow(project_id, OWNER)
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(main.generate_project_endpoint(
                    GenerateProjectRequest(prompt="Build it", project_id=project_id),
                    USER,
                ))

        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual("tool_execution_blocked_while_gathering_context", raised.exception.detail["code"])
        create_job.assert_not_called()

    def test_explicit_follow_up_answers_clear_matching_questions(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-chat-answers"
        with sqlite_repository():
            first = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "Build a compact environmental sensor."},
            )
            second = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": (
                        "Use an ESP32-S3 powered from USB-C 5 V. Show readings on an OLED. "
                        "Use a rounded desktop puck shape. It is a bench tool for engineers, must fit within 100 mm, "
                        "and should include wiring and a BOM. "
                        "Validate that readings remain within the sensor tolerance."
                    ),
                },
            )

        self.assertEqual(201, first.status_code)
        self.assertTrue(first.json()["questions"])
        self.assertEqual(201, second.status_code)
        self.assertEqual([], second.json()["questions"])

    def test_uncertain_reply_guides_without_repeating_internal_question(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-chat-guidance"
        with sqlite_repository():
            first = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Environmental monitor with sensor feedback, display, and battery power.",
                },
            )
            second = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "idk"},
            )

        self.assertEqual(201, first.status_code)
        self.assertEqual(201, second.status_code)
        question = first.json()["questions"][0]
        reply = second.json()["assistant_message"]
        self.assertNotIn(question, reply)
        self.assertIn("you don’t need to choose technical parts", reply)
        self.assertIn("build agents can propose", reply)
        self.assertNotIn("idk", second.json()["design_brief"]["requirements"])

    def test_clarification_request_is_answered_instead_of_saved_as_requirement(self) -> None:
        project_id = str(uuid.uuid4())
        conversation_id = "context-chat-explanation"
        with sqlite_repository():
            first = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={
                    "conversation_id": conversation_id,
                    "text": "Environmental monitor with sensor feedback, display, and battery power.",
                },
            )
            second = self.client.post(
                f"/projects/{project_id}/context/messages",
                json={"conversation_id": conversation_id, "text": "What do you mean fixed?"},
            )

        self.assertEqual(201, first.status_code)
        self.assertEqual(201, second.status_code)
        body = second.json()
        self.assertIn("already decided", body["assistant_message"])
        self.assertIn("no preference", body["assistant_message"])
        self.assertIn("build agents can choose", body["assistant_message"])
        self.assertNotIn("What do you mean fixed?", body["design_brief"]["requirements"])

    def test_a2a_generation_rejects_before_worker_job_is_created(self) -> None:
        project_id = str(uuid.uuid4())
        message = A2AMessage(
            sender="test-agent",
            action="forma.generate_project",
            payload={"project_id": project_id, "owner_user_id": OWNER, "prompt": "Build it"},
        )
        with sqlite_repository(), patch.object(a2a.A2A_HUB, "register", new=AsyncMock()), patch.object(
            a2a.JOB_STORE, "create_job"
        ) as create_job:
            database.initialize_project_workflow(project_id, OWNER)
            with self.assertRaises(WorkflowStateError):
                asyncio.run(a2a.submit_a2a_message(message, USER))

        create_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
