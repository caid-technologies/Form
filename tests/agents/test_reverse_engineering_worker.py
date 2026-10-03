from __future__ import annotations

import base64
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workers import (
    REVERSE_ENGINEERING_CAPABILITY_ID,
    REVERSE_ENGINEERING_INPUT_VERSION,
    REVERSE_ENGINEERING_OUTPUT_VERSION,
    REVERSE_ENGINEERING_WORKER_ID,
    WORKER_CONTRACT_VERSION,
    ReverseEngineeringWorker,
    WorkerCapability,
    WorkerDefinition,
    WorkerDependency,
    WorkerOrchestrator,
    WorkerPlanStatus,
    WorkerRegistry,
    WorkerRequest,
    WorkerResult,
    WorkerResultStatus,
    build_reverse_engineering_request,
)
from forma_core.workspaces.design_briefs import DESIGN_BRIEF_SCHEMA_VERSION, DesignBrief
from forma_core.workspaces.projects import ProjectStateError, ProjectStateService
from forma_core.workspaces.reverse_engineering import (
    ReverseEngineeringArtifactReference,
    ReverseEngineeringConfidence,
    ReverseEngineeringEvidenceSource,
    ReverseEngineeringFindingKind,
    ReverseEngineeringReport,
)
from forma_core.workspaces.workflow import (
    ProjectWorkflowService,
    ProjectWorkflowState,
    WorkflowActorType,
)


OWNER = "reverse-engineering-user"


def _context(request: WorkerRequest) -> dict[str, Any]:
    return request.model_dump(include={
        "contract_version",
        "project_id",
        "project_revision",
        "design_brief_id",
        "design_brief_version",
        "job_id",
        "correlation_id",
        "worker_id",
        "capability_id",
    })


class FindingsConsumerWorker:
    def worker_definition(self) -> WorkerDefinition:
        return WorkerDefinition(
            worker_id="findings-consumer-worker",
            name="Findings consumer fixture",
            worker_version="1.0.0",
            capabilities=[WorkerCapability(
                capability_id="findings.consume",
                description="Consume reverse-engineering findings in a downstream fixture.",
                supported_input_versions=[REVERSE_ENGINEERING_OUTPUT_VERSION],
                supported_output_versions=["consumption-receipt.v1"],
            )],
        )

    def execute(self, request: WorkerRequest, _report_progress: Any) -> WorkerResult:
        dependency = request.payload["dependency_results"]["job-reverse-engineering"]
        report = ReverseEngineeringReport.model_validate(
            dependency["output"]["reverse_engineering_report"]
        )
        return WorkerResult(
            **_context(request),
            output_contract_version="consumption-receipt.v1",
            status=WorkerResultStatus.SUCCEEDED,
            output={
                "consumed_artifact_id": report.artifact.artifact_id,
                "consumed_finding_count": len(report.findings),
                "evidence_links": sum(len(item.evidence_ids) for item in report.findings),
            },
        )


class ReverseEngineeringWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.provider = create_sqlite_provider(
            source="reverse-engineering worker test",
            url=f"sqlite:///{Path(self.directory.name) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        self.provider.initialize()
        self.repository = SqlAlchemyRepository(self.provider.session_factory)
        self.state = ProjectStateService(self.repository)
        self.workflow = ProjectWorkflowService(self.repository)
        self.project_id = uuid.uuid4()
        self.brief = self._persist_brief()
        self.workflow.initialize(str(self.project_id), OWNER)
        self.workflow.transition(
            str(self.project_id),
            OWNER,
            ProjectWorkflowState.BUILDING,
            actor_type=WorkflowActorType.USER,
            actor_id=OWNER,
            reason="Start reverse-engineering worker test.",
        )

    def tearDown(self) -> None:
        self.provider.dispose()
        self.directory.cleanup()

    def _persist_brief(self) -> DesignBrief:
        brief = DesignBrief(
            schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
            conversation_id="conversation-reverse-engineering",
            intent="Recreate a compact controller shown in the uploaded reference",
            summary="Inspect an uploaded product reference before generation.",
            requirements=["Identify visible structural evidence"],
            constraints=["Do not invent hidden connectivity"],
            requested_outputs=["wiring", "bom"],
            validation_criteria=["Every inference cites evidence"],
            readiness="ready",
            design_brief_id=uuid.uuid4(),
            project_id=self.project_id,
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

    @staticmethod
    def _image_reference(*, ambiguous: bool = False) -> ReverseEngineeringArtifactReference:
        image = Image.new("RGB", (96, 64), "white")
        if not ambiguous:
            drawing = ImageDraw.Draw(image)
            drawing.rectangle((18, 14, 77, 49), fill="navy")
            drawing.ellipse((38, 23, 57, 42), fill="orange")
        output = BytesIO()
        image.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return ReverseEngineeringArtifactReference(
            artifact_id="uploaded-controller-reference",
            kind="uploaded_image",
            uri=f"data:image/png;base64,{encoded}",
            media_type="image/png",
            label="controller-reference.png",
        )

    def _request(
        self,
        artifact: ReverseEngineeringArtifactReference,
        *,
        job_id: str = "job-reverse-engineering",
    ) -> WorkerRequest:
        return build_reverse_engineering_request(
            self.brief,
            artifact,
            project_revision=1,
            job_id=job_id,
            correlation_id="corr-reverse-engineering",
        )

    async def test_supported_image_returns_evidence_inference_and_uncertainty_without_state_write(self) -> None:
        artifact = self._image_reference()
        worker = ReverseEngineeringWorker(self.state)
        registration = WorkerRegistry([worker]).resolve(
            REVERSE_ENGINEERING_WORKER_ID,
            REVERSE_ENGINEERING_CAPABILITY_ID,
        )
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self._request(artifact)], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-reverse-engineering"].result
        report = ReverseEngineeringReport.model_validate(result.output["reverse_engineering_report"])

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(REVERSE_ENGINEERING_INPUT_VERSION, registration.capability.supported_input_versions[0])
        self.assertEqual(
            ["image/jpeg", "image/png", "image/webp"],
            registration.capability.metadata["supported_media_types"],
        )
        self.assertFalse(report.ambiguous)
        self.assertEqual((96, 64), (report.artifact.width_px, report.artifact.height_px))
        self.assertEqual(
            {ReverseEngineeringEvidenceSource.ARTIFACT, ReverseEngineeringEvidenceSource.DESIGN_BRIEF},
            {item.source for item in report.evidence},
        )
        self.assertEqual(
            {
                ReverseEngineeringFindingKind.STRUCTURE,
                ReverseEngineeringFindingKind.FUNCTION,
                ReverseEngineeringFindingKind.PROPERTY,
            },
            {item.kind for item in report.findings},
        )
        self.assertTrue(all(item.evidence_ids and item.uncertainties for item in report.findings))
        self.assertNotIn(artifact.uri, result.model_dump_json())
        with self.assertRaises(ProjectStateError):
            self.state.get_latest(self.project_id, OWNER)

    async def test_ambiguous_image_succeeds_with_low_confidence_uncertainty(self) -> None:
        worker = ReverseEngineeringWorker(self.state)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self._request(self._image_reference(ambiguous=True))], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        report = ReverseEngineeringReport.model_validate(
            completed.jobs["job-reverse-engineering"].result.output["reverse_engineering_report"]
        )
        structure = next(item for item in report.findings if item.kind == ReverseEngineeringFindingKind.STRUCTURE)

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertTrue(report.ambiguous)
        self.assertEqual(ReverseEngineeringConfidence.LOW, structure.confidence)
        self.assertIn("No distinct foreground", structure.inference)
        self.assertGreaterEqual(len(report.overall_uncertainties), 3)

    async def test_unsupported_artifact_returns_structured_nonretryable_error(self) -> None:
        artifact = ReverseEngineeringArtifactReference(
            artifact_id="uploaded-datasheet",
            kind="uploaded_document",
            uri="data:application/pdf;base64,JVBERi0xLjQ=",
            media_type="application/pdf",
        )
        worker = ReverseEngineeringWorker(self.state)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self._request(artifact)], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-reverse-engineering"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("unsupported_reverse_engineering_artifact", result.error.code)
        self.assertFalse(result.error.retryable)
        self.assertEqual(
            ["image/jpeg", "image/png", "image/webp"],
            result.error.details["context"]["supported_media_types"],
        )

    async def test_downstream_worker_consumes_persisted_dependency_result(self) -> None:
        reverse_worker = ReverseEngineeringWorker(self.state)
        consumer = FindingsConsumerWorker()
        reverse_request = self._request(self._image_reference())
        consumer_request = WorkerRequest(
            contract_version=WORKER_CONTRACT_VERSION,
            project_id=self.project_id,
            project_revision=1,
            design_brief_id=self.brief.design_brief_id,
            design_brief_version=1,
            job_id="job-findings-consumer",
            correlation_id="corr-reverse-engineering",
            worker_id="findings-consumer-worker",
            capability_id="findings.consume",
            input_contract_version=REVERSE_ENGINEERING_OUTPUT_VERSION,
            dependencies=[WorkerDependency(
                job_id="job-reverse-engineering",
                worker_id=REVERSE_ENGINEERING_WORKER_ID,
                capability_id=REVERSE_ENGINEERING_CAPABILITY_ID,
            )],
        )
        orchestrator = WorkerOrchestrator(
            self.repository,
            [reverse_worker, consumer],
            workflow_service=self.workflow,
        )
        plan = orchestrator.create_plan([reverse_request, consumer_request], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        receipt = completed.jobs["job-findings-consumer"].result.output

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual("uploaded-controller-reference", receipt["consumed_artifact_id"])
        self.assertEqual(3, receipt["consumed_finding_count"])
        self.assertGreaterEqual(receipt["evidence_links"], 3)


if __name__ == "__main__":
    unittest.main()
