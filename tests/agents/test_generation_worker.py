from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from forma_core.agents.orchestrator import HardwarePipelineOrchestrator, extract_power_rails
from forma_core.agents.pipeline import emit_agent_pipeline_event
from forma_core.database import list_project_generation_jobs
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workers import (
    GENERATION_CAPABILITY_ID,
    GENERATION_INPUT_VERSION,
    GENERATION_OUTPUT_VERSION,
    GENERATION_WORKER_ID,
    WORKER_CONTRACT_VERSION,
    GenerationWorker,
    HardwareIntermediateRepresentationGenerationEngine,
    OrchestrationTaskStatus,
    WorkerOrchestrator,
    WorkerPlanStatus,
    WorkerRegistry,
    WorkerRequest,
    build_generation_draft,
)
from forma_core.workspaces.design_briefs import (
    DESIGN_BRIEF_SCHEMA_VERSION,
    DesignBrief,
    DesignBriefReference,
)
from forma_core.workspaces.projects import (
    ProjectArtifact,
    ProjectRevisionDraft,
    ProjectStateError,
    ProjectStateService,
    ProjectSystem,
)
from forma_core.workspaces.projects.models import (
    BusConnection,
    ComponentInstance,
    ConnectionNet,
    HardwareIntermediateRepresentation,
    PinReference,
    PowerRail,
    ProjectOverview,
)
from forma_core.workspaces.workflow import (
    ProjectWorkflowService,
    ProjectWorkflowState,
    WorkflowActorType,
)


OWNER = "generation-worker-user"


class FakeGenerationEngine:
    def __init__(
        self,
        *,
        fail: bool = False,
        target_project_id: str | None = None,
        assembly_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.fail = fail
        self.target_project_id = target_project_id
        self.assembly_metadata = assembly_metadata or {}
        self.received: list[DesignBrief] = []

    def generate(self, design_brief: DesignBrief) -> ProjectRevisionDraft:
        self.received.append(design_brief)
        if self.fail:
            raise TimeoutError("generation provider timed out")
        component = ComponentInstance(
            ref_des="U1",
            part_number="ESP32-DEVKIT",
            name="ESP32 controller",
            category="Microcontroller",
            rationale="Provides processing and connectivity required by the frozen brief.",
        )
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Frozen Brief Controller",
                description=design_brief.summary,
                difficulty="Intermediate",
                category="IoT",
            ),
            components=[component],
            assembly_metadata={
                "project_id": self.target_project_id or str(design_brief.project_id),
                "revision": 1,
                **self.assembly_metadata,
            },
        )
        return ProjectRevisionDraft(
            state=state,
            components=[component],
            systems=[
                ProjectSystem(
                    system_id="controller-system",
                    kind="control",
                    name="Main controller",
                    component_refs=["U1"],
                )
            ],
            artifacts=[
                ProjectArtifact(
                    artifact_id="initial-state",
                    kind="project-state",
                    uri=f"forma://projects/{design_brief.project_id}/revisions/1/state",
                    media_type="application/vnd.forma.hardware-ir+json",
                )
            ],
            assumptions=[*design_brief.assumptions, "Use the ESP32 development-board regulator."],
        )


class ProviderDeadlineError(RuntimeError):
    code = 504


class ProviderDeadlineGenerationEngine(FakeGenerationEngine):
    def generate(self, design_brief: DesignBrief) -> ProjectRevisionDraft:
        del design_brief
        raise ProviderDeadlineError("504 DEADLINE_EXCEEDED: Deadline expired before operation could complete.")


class ObservableGenerationEngine(HardwareIntermediateRepresentationGenerationEngine):
    def generate(self, design_brief: DesignBrief) -> ProjectRevisionDraft:
        emit_agent_pipeline_event(None, "intent_parser", "started")
        emit_agent_pipeline_event(None, "intent_parser", "completed")
        return FakeGenerationEngine().generate(design_brief)


class CancellableGenerationEngine(HardwareIntermediateRepresentationGenerationEngine):
    def __init__(self, cancel: Any) -> None:
        super().__init__()
        self.cancel = cancel

    def generate(self, design_brief: DesignBrief) -> ProjectRevisionDraft:
        emit_agent_pipeline_event(None, "intent_parser", "started")
        self.cancel()
        emit_agent_pipeline_event(None, "intent_parser", "completed")
        return FakeGenerationEngine().generate(design_brief)


class RetryableStagedGenerationEngine(HardwareIntermediateRepresentationGenerationEngine):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        design_brief: DesignBrief,
        *,
        generation_metadata: dict[str, Any] | None = None,
    ) -> ProjectRevisionDraft:
        metadata = dict(generation_metadata or {})
        self.calls.append(metadata)
        first_attempt = len(self.calls) == 1
        generation_run = {
            "generation_run_id": "default-retry-run",
            "workflow": "default",
            "retry_stage": metadata.get("retry_stage"),
            "status": "partial" if first_attempt else "succeeded",
            "records": {
                "component_selection": {
                    "stage_id": "component_selection",
                    "status": "succeeded",
                    "dependencies": [],
                },
                "wiring_netlist": {
                    "stage_id": "wiring_netlist",
                    "status": "failed" if first_attempt else "succeeded",
                    "dependencies": ["component_selection"],
                },
                "package_project": {
                    "stage_id": "package_project",
                    "status": "succeeded",
                    "dependencies": [],
                },
            },
        }
        checkpoint = metadata.get("stage_checkpoint")
        if first_attempt and callable(checkpoint):
            for stage_id in ("component_selection", "wiring_netlist", "package_project"):
                checkpoint({
                    "generation_run_id": generation_run["generation_run_id"],
                    "workflow": "default",
                    "retry_stage": None,
                    "status": generation_run["status"],
                    "record": generation_run["records"][stage_id],
                })
        return FakeGenerationEngine(assembly_metadata={
            "generation_status": "partial" if first_attempt else "succeeded",
            "project_readiness": "partial" if first_attempt else "complete",
            "generation_run": generation_run,
            "generation_stage_failures": ([{
                "stage_id": "wiring_netlist",
                "status": "failed",
            }] if first_attempt else []),
        }).generate(design_brief)


class GenerationWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.provider = create_sqlite_provider(
            source="generation worker test",
            url=f"sqlite:///{Path(self.directory.name) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        self.provider.initialize()
        self.repository = SqlAlchemyRepository(self.provider.session_factory)
        self.state = ProjectStateService(self.repository)
        self.workflow = ProjectWorkflowService(self.repository)
        self.project_id = uuid.uuid4()
        self.brief = self._persist_brief(self.project_id)
        self.workflow.initialize(str(self.project_id), OWNER)
        self.workflow.transition(
            str(self.project_id),
            OWNER,
            ProjectWorkflowState.BUILDING,
            actor_type=WorkflowActorType.USER,
            actor_id=OWNER,
            reason="Start Generation worker test.",
        )

    def test_hardware_engine_generates_a_product_image_by_default(self) -> None:
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Vertex Image Project",
                description="A project that needs a product render.",
                difficulty="Beginner",
                category="IoT",
            ),
            assembly_metadata={"project_id": str(self.project_id)},
        )
        with (
            patch("forma_core.agents.orchestrator.HardwarePipelineOrchestrator") as orchestrator_type,
            patch("forma_core.workers.generation.attach_product_image") as attach_image,
        ):
            orchestrator_type.return_value.generate_project.return_value = state

            draft = HardwareIntermediateRepresentationGenerationEngine().generate(self.brief)

        attach_image.assert_called_once()
        self.assertTrue(attach_image.call_args.kwargs["generate_image"])
        self.assertEqual("Vertex Image Project", draft.state.overview.title)

    def test_hardware_engine_copies_uploaded_reference_onto_the_project(self) -> None:
        brief = self.brief.model_copy(update={
            "references": [
                DesignBriefReference(
                    reference_id="clipboard-image",
                    kind="uploaded_image",
                    label="hardware-reference.png",
                    media_type="image/png",
                    metadata={
                        "data_url": "data:image/png;base64,aW1hZ2U=",
                        "inline_data_supplied": True,
                    },
                )
            ]
        })
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Reference Image Project",
                description="A project started from a hardware photo.",
                difficulty="Beginner",
                category="IoT",
            ),
            assembly_metadata={"project_id": str(self.project_id)},
        )
        with (
            patch("forma_core.agents.orchestrator.HardwarePipelineOrchestrator") as orchestrator_type,
            patch("forma_core.workers.generation.attach_product_image"),
            patch("forma_core.workers.generation.attach_hardware_reference_image") as attach_reference,
        ):
            orchestrator_type.return_value.generate_project.return_value = state
            HardwareIntermediateRepresentationGenerationEngine().generate(brief)

        attach_reference.assert_called_once()
        self.assertEqual(state, attach_reference.call_args.args[0])
        self.assertEqual("data:image/png;base64,aW1hZ2U=", attach_reference.call_args.args[1])
        generate_project = orchestrator_type.return_value.generate_project
        prompt = generate_project.call_args.args[0]
        self.assertNotIn("aW1hZ2U=", prompt)
        self.assertIn("inline_data_supplied", prompt)
        self.assertEqual(b"image", generate_project.call_args.kwargs["image_bytes"])
        self.assertEqual("image/png", generate_project.call_args.kwargs["image_mime_type"])

    def test_hardware_engine_returns_contained_staged_root_failure(self) -> None:
        state = HardwareIntermediateRepresentation(assembly_metadata={
            "generation_status": "failed",
            "status": "failed",
            "generation_error": {"type": "TimeoutError", "message": "architecture timed out"},
            "generation_run": {"generation_run_id": "failed-run", "records": {}},
        })
        with (
            patch("forma_core.agents.orchestrator.HardwarePipelineOrchestrator") as orchestrator_type,
            patch("forma_core.workers.generation.attach_product_image"),
        ):
            orchestrator_type.return_value.generate_project.return_value = state

            draft = HardwareIntermediateRepresentationGenerationEngine().generate(self.brief)

        self.assertEqual("failed", draft.state.assembly_metadata["generation_status"])

    def test_hardware_ir_rejects_dangling_component_references(self) -> None:
        component = ComponentInstance(
            ref_des="U1",
            part_number="ESP32-DEVKIT",
            name="ESP32 controller",
            category="Microcontroller",
            rationale="Provides processing and connectivity.",
        )
        with self.assertRaisesRegex(ValueError, "unknown component instance 'R2'"):
            HardwareIntermediateRepresentation(
                components=[component],
                nets=[
                    ConnectionNet(
                        net_id="NET_I2C_SDA",
                        name="I2C data",
                        net_type="I2C",
                        pins=[
                            PinReference(ref_des="U1", pin_id="SDA"),
                            PinReference(ref_des="R2", pin_id="1"),
                        ],
                    )
                ],
                buses=[BusConnection(bus_id="I2C_1", bus_type="I2C", nets=["NET_I2C_SDA"])],
                power_rails=[
                    PowerRail(
                        rail_id="3V3",
                        voltage=3.3,
                        max_current_capacity_ma=500,
                        source_component="R2",
                    )
                ],
            )

    def test_generation_draft_keeps_same_voltage_power_nets_as_unique_systems(self) -> None:
        component = ComponentInstance(
            ref_des="U1",
            part_number="ESP32-DEVKIT",
            name="ESP32 controller",
            category="Microcontroller",
            rationale="Provides processing and connectivity.",
        )
        nets = [
            ConnectionNet(
                net_id="NET_VCC_5V",
                name="Always-on logic power",
                net_type="Power",
                voltage=5.0,
                pins=[PinReference(ref_des="U1", pin_id="VIN")],
            ),
            ConnectionNet(
                net_id="NET_SERVO_VCC",
                name="Switched servo power",
                net_type="Power",
                voltage=5.0,
                pins=[PinReference(ref_des="U1", pin_id="VIN")],
            ),
        ]
        rails = extract_power_rails([component], nets)
        state = HardwareIntermediateRepresentation(components=[component], nets=nets, power_rails=rails)

        draft = build_generation_draft(self.brief, state)

        self.assertEqual(["RAIL_NET_VCC_5V", "RAIL_NET_SERVO_VCC"], [rail.rail_id for rail in rails])
        self.assertEqual(
            ["system-primary", "power-RAIL_NET_VCC_5V", "power-RAIL_NET_SERVO_VCC"],
            [system.system_id for system in draft.systems],
        )

    def test_generation_draft_reuses_persisted_stage_artifact_references(self) -> None:
        stage_artifact = ProjectArtifact(
            artifact_id="run-1:web_architect:attempt:1",
            kind="generation-stage:web_architect",
            uri="forma://generation-runs/run-1/stages/web_architect/attempts/1",
            media_type="application/json",
            checksum="sha256:abc123",
        )
        state = HardwareIntermediateRepresentation(assembly_metadata={
            "generation_run": {
                "records": {
                    "web_architect": {
                        "status": "succeeded",
                        "artifact": stage_artifact.model_dump(mode="json"),
                    }
                }
            }
        })

        draft = build_generation_draft(self.brief, state)

        self.assertIn(stage_artifact, draft.artifacts)

    def test_generation_draft_disambiguates_duplicate_power_and_bus_system_ids(self) -> None:
        component = ComponentInstance(
            ref_des="U1",
            part_number="ESP32-DEVKIT",
            name="ESP32 controller",
            category="Microcontroller",
            rationale="Provides processing and connectivity.",
        )
        state = HardwareIntermediateRepresentation(
            components=[component],
            power_rails=[
                PowerRail(rail_id="RAIL_5V", voltage=5.0, max_current_capacity_ma=1000, source_component="U1"),
                PowerRail(rail_id="RAIL_5V", voltage=5.0, max_current_capacity_ma=500, source_component="U1"),
            ],
            buses=[
                BusConnection(bus_id="I2C_1", bus_type="I2C", nets=[]),
                BusConnection(bus_id="I2C_1", bus_type="I2C", nets=[]),
            ],
        )

        draft = build_generation_draft(self.brief, state)

        self.assertEqual(
            ["system-primary", "power-RAIL_5V", "power-RAIL_5V-2", "bus-I2C_1", "bus-I2C_1-2"],
            [system.system_id for system in draft.systems],
        )

    def tearDown(self) -> None:
        self.provider.dispose()
        self.directory.cleanup()

    def _persist_brief(self, project_id: uuid.UUID) -> DesignBrief:
        brief = DesignBrief(
            schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
            conversation_id="conversation-generation",
            intent="Build a compact sensor controller",
            summary="A USB-powered sensor controller with a small display.",
            requirements=["Read one digital sensor", "Show status on a display"],
            constraints=["Use 5 V USB input"],
            references=[
                DesignBriefReference(
                    reference_id="ref-datasheet",
                    kind="datasheet",
                    label="Declared controller datasheet",
                    uri="https://example.test/declared-datasheet.pdf",
                    media_type="application/pdf",
                )
            ],
            requested_outputs=["wiring", "bom"],
            validation_criteria=["Controller remains within input voltage limits"],
            assumptions=["The display uses I2C"],
            readiness="ready",
            design_brief_id=uuid.uuid4(),
            project_id=project_id,
            brief_version=1,
            created_at=datetime.now(timezone.utc),
        )
        self.repository.insert_design_brief_version({
            "id": str(uuid.uuid4()),
            "design_brief_id": str(brief.design_brief_id),
            "project_id": str(brief.project_id),
            "conversation_id": brief.conversation_id,
            "owner_user_id": OWNER,
            "brief_version": brief.brief_version,
            "schema_version": brief.schema_version,
            "previous_version": brief.previous_version,
            "payload_json": brief.model_dump(mode="json"),
            "created_at": brief.created_at.isoformat(),
        })
        return brief

    def request(self, *, payload: dict[str, Any] | None = None, revision: int = 1) -> WorkerRequest:
        return WorkerRequest(
            contract_version=WORKER_CONTRACT_VERSION,
            project_id=self.project_id,
            project_revision=revision,
            design_brief_id=self.brief.design_brief_id,
            design_brief_version=self.brief.brief_version,
            job_id="job-generation-initial",
            correlation_id="corr-generation-build",
            worker_id=GENERATION_WORKER_ID,
            capability_id=GENERATION_CAPABILITY_ID,
            input_contract_version=GENERATION_INPUT_VERSION,
            payload=payload or {"design_brief": self.brief.model_dump(mode="json")},
        )

    async def test_registered_worker_persists_initial_revision_through_orchestrator(self) -> None:
        engine = FakeGenerationEngine()
        publisher = Mock(return_value=str(self.project_id))
        worker = GenerationWorker(self.state, engine, project_publisher=publisher)
        registry = WorkerRegistry([worker])
        orchestrator = WorkerOrchestrator(
            self.repository,
            [worker],
            workflow_service=self.workflow,
        )
        plan = orchestrator.create_plan([self.request()], OWNER, max_concurrency=1)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        revision = self.state.get_latest(self.project_id, OWNER)
        result = completed.jobs["job-generation-initial"].result

        self.assertEqual(GENERATION_OUTPUT_VERSION, registry.resolve(
            GENERATION_WORKER_ID, GENERATION_CAPABILITY_ID
        ).capability.supported_output_versions[0])
        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(OrchestrationTaskStatus.SUCCEEDED, completed.jobs["job-generation-initial"].status)
        self.assertEqual(1, revision.revision)
        self.assertEqual(self.brief.design_brief_id, revision.design_brief_id)
        self.assertEqual(self.brief.brief_version, revision.design_brief_version)
        self.assertEqual(["U1"], [item.ref_des for item in revision.components])
        self.assertEqual(["controller-system"], [item.system_id for item in revision.systems])
        self.assertEqual(["initial-state"], [item.artifact_id for item in revision.artifacts])
        self.assertEqual(
            ["The display uses I2C", "Use the ESP32 development-board regulator."],
            revision.assumptions,
        )
        self.assertEqual(["initial-state"], [item.artifact_id for item in result.artifacts])
        self.assertEqual(revision.model_dump(mode="json"), result.output["project_revision"])
        self.assertEqual(ProjectWorkflowState.AWAITING_FEEDBACK, self.workflow.get(str(self.project_id), OWNER).state)
        self.assertEqual(self.brief, engine.received[0])
        self.assertEqual(["ref-datasheet"], [item.reference_id for item in engine.received[0].references])
        publisher.assert_called_once_with(revision, self.brief, OWNER)

    async def test_idempotent_replay_retries_the_gallery_projection(self) -> None:
        engine = FakeGenerationEngine()
        publisher = Mock(side_effect=[RuntimeError("gallery unavailable"), str(self.project_id)])
        worker = GenerationWorker(self.state, engine, project_publisher=publisher)
        request = self.request().model_copy(update={"metadata": {"execution_owner_user_id": OWNER}})

        async def progress(_: Any) -> None:
            return None

        first = await worker.execute(request, progress)
        second = await worker.execute(request, progress)

        self.assertEqual("failed", first.status.value)
        self.assertTrue(first.error.retryable)
        self.assertIn("gallery unavailable", first.error.message)
        self.assertEqual("succeeded", second.status.value)
        self.assertTrue(second.output["idempotent_replay"])
        self.assertEqual(2, publisher.call_count)
        self.assertEqual(1, len(engine.received))

    async def test_partial_default_generation_retry_appends_revision_and_reuses_run(self) -> None:
        engine = RetryableStagedGenerationEngine()
        worker = GenerationWorker(self.state, engine)
        orchestrator = WorkerOrchestrator(
            self.repository,
            [worker],
            workflow_service=self.workflow,
        )
        plan = orchestrator.create_plan([self.request()], OWNER)

        partial = await orchestrator.execute(plan.plan_id, OWNER)

        self.assertEqual(WorkerPlanStatus.PARTIAL, partial.status)
        self.assertEqual(1, self.state.get_latest(self.project_id, OWNER).revision)
        reset = await orchestrator.reset(plan.plan_id, OWNER)
        self.assertEqual("wiring_netlist", reset.jobs["job-generation-initial"].request.metadata["retry_stage"])
        preserved_checkpoints = [
            progress.metadata["generation_stage_checkpoint"]["record"]["stage_id"]
            for progress in reset.jobs["job-generation-initial"].progress
            if "generation_stage_checkpoint" in progress.metadata
        ]
        self.assertEqual(["component_selection"], preserved_checkpoints)

        completed = await orchestrator.execute(plan.plan_id, OWNER)

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(2, self.state.get_latest(self.project_id, OWNER).revision)
        self.assertEqual("wiring_netlist", engine.calls[1]["retry_stage"])
        self.assertEqual(
            "default-retry-run",
            engine.calls[1]["prior_generation_run"]["generation_run_id"],
        )

    async def test_project_chat_iteration_appends_an_immutable_revision(self) -> None:
        engine = FakeGenerationEngine()
        worker = GenerationWorker(self.state, engine)
        request = self.request().model_copy(update={"metadata": {"execution_owner_user_id": OWNER}})

        async def progress(_: Any) -> None:
            return None

        first = await worker.execute(request, progress)
        first_revision = self.state.get_latest(self.project_id, OWNER)
        revised_state = first_revision.state.model_copy(deep=True)
        revised_state.overview.title = "Revised Controller"
        draft = ProjectRevisionDraft(
            state=revised_state,
            components=list(revised_state.components),
            systems=list(first_revision.systems),
            artifacts=list(first_revision.artifacts),
            assumptions=list(first_revision.assumptions),
        )
        second = self.state.create_revision(
            draft,
            project_id=self.project_id,
            owner_user_id=OWNER,
            source_job_id="iteration-job-1",
        ).revision

        self.assertEqual("succeeded", first.status.value)
        self.assertEqual(2, second.revision)
        self.assertEqual(1, second.parent_revision)
        self.assertEqual("Revised Controller", self.state.get_latest(self.project_id, OWNER).state.overview.title)

    async def test_hardware_pipeline_events_are_reported_as_worker_progress(self) -> None:
        worker = GenerationWorker(self.state, ObservableGenerationEngine())
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self.request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        task = completed.jobs["job-generation-initial"]
        reported = task.progress

        pipeline_events = [
            item.metadata["pipeline_event"]
            for item in reported
            if "pipeline_event" in item.metadata
        ]
        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(["started", "completed"], [event["status"] for event in pipeline_events])
        self.assertEqual(["intent_parser", "intent_parser"], [event["step_id"] for event in pipeline_events])
        self.assertEqual(sorted(item.sequence for item in reported), [item.sequence for item in reported])
        self.assertEqual(len({item.sequence for item in reported}), len(reported))

    async def test_cancelled_hardware_pipeline_does_not_persist_a_revision(self) -> None:
        cancellation = {"requested": False}
        worker = GenerationWorker(
            self.state,
            CancellableGenerationEngine(lambda: cancellation.update(requested=True)),
        )
        orchestrator = WorkerOrchestrator(
            self.repository,
            [worker],
            workflow_service=self.workflow,
            cancellation_check=lambda: cancellation["requested"],
        )
        plan = orchestrator.create_plan([self.request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        task = completed.jobs["job-generation-initial"]

        self.assertEqual(WorkerPlanStatus.CANCELLED, completed.status)
        self.assertEqual(OrchestrationTaskStatus.CANCELLED, task.status)
        self.assertEqual("generation_cancelled", task.error.code)
        self.assertEqual(ProjectWorkflowState.AWAITING_FEEDBACK, self.workflow.get(str(self.project_id), OWNER).state)
        with self.assertRaises(ProjectStateError):
            self.state.get_latest(self.project_id, OWNER)

    async def test_provider_failure_is_structured_retryable_and_does_not_create_revision(self) -> None:
        engine = FakeGenerationEngine(fail=True)
        worker = GenerationWorker(self.state, engine)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self.request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-generation-initial"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("generation_failed", result.error.code)
        self.assertTrue(result.error.retryable)
        self.assertIn("timed out", result.error.message)
        with self.assertRaises(ProjectStateError) as missing:
            self.state.get_latest(self.project_id, OWNER)
        self.assertEqual("project_revision_not_found", missing.exception.code)

    async def test_admin_job_projection_includes_image_failure_output(self) -> None:
        engine = FakeGenerationEngine(assembly_metadata={
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": "Image provider returned no images.",
            "image_output_error_type": "empty_response",
            "operation_statuses": [
                {"id": "image_generation", "label": "Image generation", "status": "failed"},
            ],
        })
        worker = GenerationWorker(self.state, engine)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self.request()], OWNER)
        await orchestrator.execute(plan.plan_id, OWNER)

        with patch("forma_core.database._DATABASE_REPOSITORY", self.repository):
            jobs = list_project_generation_jobs(limit=10)

        projected = next(job for job in jobs if job["job_id"] == "job-generation-initial")
        summary = projected["result_summary"]
        self.assertTrue(summary["image_output_failed"])
        self.assertEqual("failed", summary["image_output_status"])
        self.assertEqual("Image provider returned no images.", summary["image_output_error"])
        self.assertEqual("failed", summary["operation_statuses"][0]["status"])

    async def test_numeric_provider_status_is_preserved_as_a_retryable_worker_error(self) -> None:
        worker = GenerationWorker(self.state, ProviderDeadlineGenerationEngine())
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self.request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-generation-initial"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("504", result.error.code)
        self.assertTrue(result.error.retryable)
        self.assertIn("DEADLINE_EXCEEDED", result.error.message)
        self.assertEqual("ProviderDeadlineError", result.error.details["exception_type"])

    async def test_extra_conversation_input_is_rejected_before_generation(self) -> None:
        engine = FakeGenerationEngine()
        worker = GenerationWorker(self.state, engine)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        request = self.request(payload={
            "design_brief": self.brief.model_dump(mode="json"),
            "conversation": "undeclared raw chat must not reach generation",
        })
        plan = orchestrator.create_plan([request], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-generation-initial"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("invalid_generation_request", result.error.code)
        self.assertFalse(result.error.retryable)
        self.assertEqual([], engine.received)

    async def test_modified_copy_of_frozen_brief_is_rejected_before_generation(self) -> None:
        engine = FakeGenerationEngine()
        worker = GenerationWorker(self.state, engine)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        modified = self.brief.model_copy(update={"summary": "Tampered summary"})
        plan = orchestrator.create_plan(
            [self.request(payload={"design_brief": modified.model_dump(mode="json")})],
            OWNER,
        )

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-generation-initial"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("frozen_design_brief_mismatch", result.error.code)
        self.assertFalse(result.error.retryable)
        self.assertEqual([], engine.received)

    async def test_generated_state_cannot_target_a_different_project(self) -> None:
        other_project_id = str(uuid.uuid4())
        engine = FakeGenerationEngine(target_project_id=other_project_id)
        worker = GenerationWorker(self.state, engine)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self.request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-generation-initial"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("project_revision_identity_mismatch", result.error.code)
        self.assertFalse(result.error.retryable)
        with self.assertRaises(ProjectStateError):
            self.state.get_latest(self.project_id, OWNER)
        with self.assertRaises(ProjectStateError):
            self.state.get_latest(other_project_id, OWNER)

    async def test_source_job_replay_returns_the_same_revision_without_duplicate_write(self) -> None:
        engine = FakeGenerationEngine()
        worker = GenerationWorker(self.state, engine)
        request = self.request().model_copy(update={"metadata": {"execution_owner_user_id": OWNER}})

        async def progress(_: Any) -> None:
            return None

        first = await worker.execute(request, progress)
        second = await worker.execute(request, progress)
        persisted = self.state.get_latest(self.project_id, OWNER)

        self.assertEqual("succeeded", first.status.value)
        self.assertEqual("succeeded", second.status.value)
        self.assertFalse(first.output["idempotent_replay"])
        self.assertTrue(second.output["idempotent_replay"])
        self.assertEqual(first.output["project_revision"]["revision_id"], second.output["project_revision"]["revision_id"])
        self.assertEqual(str(persisted.revision_id), second.output["project_revision"]["revision_id"])
        self.assertEqual(1, len(engine.received))

    @patch("forma_core.agents.orchestrator.ensure_agent_pipeline_active")
    @patch("forma_core.agents.orchestrator.persist_legacy_project_projection")
    def test_generation_engine_mode_disables_legacy_direct_project_write(
        self,
        persist_legacy_project_projection: Any,
        _ensure_pipeline: Any,
    ) -> None:
        pipeline = HardwarePipelineOrchestrator.__new__(HardwarePipelineOrchestrator)
        pipeline.persist_project = False
        pipeline._active_generation_metadata = {"project_id": str(self.project_id)}
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Boundary-only generation",
                description="Must be persisted by ProjectStateService.",
                difficulty="Intermediate",
                category="IoT",
            )
        )

        project_id = pipeline.save_project_to_db("frozen brief prompt", state)

        self.assertEqual(str(self.project_id), project_id)
        self.assertEqual(str(self.project_id), state.assembly_metadata["project_id"])
        persist_legacy_project_projection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
