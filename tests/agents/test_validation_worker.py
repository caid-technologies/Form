from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forma_core.persistence.models import DBProjectRevision
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workers import (
    VALIDATION_CAPABILITY_ID,
    VALIDATION_INPUT_VERSION,
    VALIDATION_WORKER_ID,
    WORKER_CONTRACT_VERSION,
    ValidationWorker,
    WorkerOrchestrator,
    WorkerPlanStatus,
    WorkerRegistry,
    WorkerRequest,
    build_validation_request,
)
from forma_core.workspaces.design_briefs import DESIGN_BRIEF_SCHEMA_VERSION, DesignBrief
from forma_core.workspaces.projects import (
    ProjectArtifact,
    ProjectRevision,
    ProjectRevisionDraft,
    ProjectStateService,
)
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    HardwareIntermediateRepresentation,
    ProjectOverview,
    ValidationIssue,
    ValidationSummary,
)
from forma_core.workspaces.validation import (
    ValidationCheckStatus,
    ValidationFindingDraft,
    ValidationFindingKind,
    ValidationReportDraft,
    ValidationReportService,
    ValidationSeverity,
)
from forma_core.workspaces.workflow import (
    ProjectWorkflowService,
    ProjectWorkflowState,
    WorkflowActorType,
)


OWNER = "validation-worker-user"


class CountingValidationEngine:
    def __init__(self, draft: ValidationReportDraft | None = None, *, fail: bool = False) -> None:
        self.draft = draft
        self.fail = fail
        self.calls = 0

    def validate(
        self,
        revision: ProjectRevision,
        design_brief: DesignBrief,
        requested_checks: list[str],
    ) -> ValidationReportDraft:
        self.calls += 1
        if self.fail:
            raise TimeoutError("validation provider timed out")
        if self.draft is not None:
            return self.draft
        raise AssertionError("CountingValidationEngine requires a draft")


class ValidationWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.provider = create_sqlite_provider(
            source="validation worker test",
            url=f"sqlite:///{Path(self.directory.name) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        self.provider.initialize()
        self.session_factory = self.provider.session_factory
        self.repository = SqlAlchemyRepository(self.provider.session_factory)
        self.state = ProjectStateService(self.repository)
        self.reports = ValidationReportService(self.repository)
        self.workflow = ProjectWorkflowService(self.repository)
        self.project_id = uuid.uuid4()
        self.brief = self._persist_brief()
        self.revision = self._persist_initial_revision()
        self.workflow.initialize(str(self.project_id), OWNER)
        self.workflow.transition(
            str(self.project_id),
            OWNER,
            ProjectWorkflowState.BUILDING,
            actor_type=WorkflowActorType.USER,
            actor_id=OWNER,
            reason="Start Validation worker test.",
        )

    def tearDown(self) -> None:
        self.provider.dispose()
        self.directory.cleanup()

    def _persist_brief(self) -> DesignBrief:
        brief = DesignBrief(
            schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
            conversation_id="conversation-validation",
            intent="Build a safe USB controller",
            summary="A USB-powered controller with one component.",
            requirements=["Control one output"],
            constraints=["Use 5 V USB input"],
            requested_outputs=["wiring", "bom"],
            validation_criteria=[
                "At least one component is selected",
                "No critical validation issues remain",
                "Enclosure survives a lunar dust cyclone",
            ],
            unresolved_questions=[],
            assumptions=[],
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

    def _draft(self) -> ProjectRevisionDraft:
        component = ComponentInstance(
            ref_des="U1",
            part_number="TEST-MCU",
            name="Test controller",
            category="Microcontroller",
            rationale="Provides the requested control function.",
        )
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Validation fixture",
                description="Fixture with deliberately mixed validation results.",
                difficulty="Intermediate",
                category="IoT",
            ),
            components=[component],
            constraints=["Use 5 V USB input"],
            validation=ValidationSummary(critical=[ValidationIssue(
                severity="CRITICAL",
                category="Safety Block",
                description="Output protection has not been selected.",
                troubleshooting="Add output protection.",
            )]),
            is_valid=False,
        )
        return ProjectRevisionDraft(
            state=state,
            components=[component],
            artifacts=[ProjectArtifact(
                artifact_id="state-r1",
                kind="project-state",
                uri=f"forma://projects/{self.project_id}/revisions/1/state",
            )],
            assumptions=["The actuator draws less than 500 mA."],
        )

    def _persist_initial_revision(self) -> ProjectRevision:
        return self.state.create_initial_revision(
            self._draft(),
            project_id=self.project_id,
            owner_user_id=OWNER,
            design_brief_id=self.brief.design_brief_id,
            design_brief_version=1,
            source_job_id="job-generation-r1",
        ).revision

    def _request(
        self,
        *,
        job_id: str = "job-validation-r1",
        revision: int = 1,
        payload: dict[str, Any] | None = None,
    ) -> WorkerRequest:
        return WorkerRequest(
            contract_version=WORKER_CONTRACT_VERSION,
            project_id=self.project_id,
            project_revision=revision,
            design_brief_id=self.brief.design_brief_id,
            design_brief_version=1,
            job_id=job_id,
            correlation_id="corr-validation",
            worker_id=VALIDATION_WORKER_ID,
            capability_id=VALIDATION_CAPABILITY_ID,
            input_contract_version=VALIDATION_INPUT_VERSION,
            payload=payload or {},
        )

    def _persist_revision_two(self) -> ProjectRevision:
        payload = self.revision.model_dump(mode="json")
        payload.update({
            "revision_id": str(uuid.uuid4()),
            "revision": 2,
            "parent_revision": 1,
            "source_job_id": "job-edit-r2",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        payload["state"]["assembly_metadata"].update({"revision": 2, "source_job_id": "job-edit-r2"})
        payload["assumptions"] = []
        revision = ProjectRevision.model_validate(payload)
        with self.session_factory() as session:
            session.add(DBProjectRevision(
                id=str(revision.revision_id),
                project_id=str(revision.project_id),
                owner_user_id=OWNER,
                revision=2,
                parent_revision=1,
                design_brief_id=str(revision.design_brief_id),
                design_brief_version=revision.design_brief_version,
                source_job_id=revision.source_job_id,
                payload_json=revision.model_dump(mode="json"),
                created_at=revision.created_at.isoformat(),
            ))
            session.commit()
        return revision

    async def test_worker_persists_revision_bound_pass_warning_failure_and_skipped_findings(self) -> None:
        worker = ValidationWorker(self.state, self.reports)
        registration = WorkerRegistry([worker]).resolve(VALIDATION_WORKER_ID, VALIDATION_CAPABILITY_ID)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self._request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-validation-r1"].result
        report = self.reports.get_by_source_job(self.project_id, OWNER, "job-validation-r1")

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(VALIDATION_INPUT_VERSION, registration.capability.supported_input_versions[0])
        self.assertIsNotNone(report)
        self.assertEqual(1, report.project_revision)
        self.assertEqual(self.brief.design_brief_id, report.design_brief_id)
        self.assertGreater(report.summary.passed, 0)
        self.assertGreater(report.summary.warnings, 0)
        self.assertGreater(report.summary.failed, 0)
        self.assertGreater(report.summary.skipped, 0)
        self.assertEqual(str(report.report_id), result.output["validation_report"]["report_id"])
        self.assertEqual("validation-report", result.artifacts[0].kind)
        for finding in report.findings:
            self.assertEqual(1, finding.project_revision)
            self.assertTrue(finding.criterion)
            self.assertTrue(finding.evidence)
            self.assertTrue(finding.remediation)
            self.assertIn(finding.severity, list(ValidationSeverity))

    async def test_exact_missing_revision_fails_without_attaching_to_latest(self) -> None:
        worker = ValidationWorker(self.state, self.reports)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self._request(revision=2)], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-validation-r1"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("project_revision_not_found", result.error.code)
        self.assertIsNone(self.reports.get_by_source_job(self.project_id, OWNER, "job-validation-r1"))

    async def test_source_job_replay_returns_same_report_without_rerunning_engine(self) -> None:
        draft = ValidationReportDraft(findings=[ValidationFindingDraft(
            criterion_id="fixture:pass",
            criterion="Fixture check passes",
            kind=ValidationFindingKind.REQUESTED_CHECK,
            status=ValidationCheckStatus.PASSED,
            severity=ValidationSeverity.INFO,
            evidence=["The fixture passed."],
            remediation="No action required.",
        )])
        engine = CountingValidationEngine(draft)
        worker = ValidationWorker(self.state, self.reports, engine)
        request = self._request().model_copy(update={"metadata": {"execution_owner_user_id": OWNER}})

        async def progress(_: Any) -> None:
            return None

        first = await worker.execute(request, progress)
        second = await worker.execute(request, progress)

        self.assertEqual("succeeded", first.status.value)
        self.assertEqual("succeeded", second.status.value)
        self.assertFalse(first.output["idempotent_replay"])
        self.assertTrue(second.output["idempotent_replay"])
        self.assertEqual(first.output["validation_report"]["report_id"], second.output["validation_report"]["report_id"])
        self.assertEqual(1, engine.calls)

    async def test_downstream_request_revalidates_changed_revision_without_moving_old_report(self) -> None:
        worker = ValidationWorker(self.state, self.reports)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        first_plan = orchestrator.create_plan([self._request()], OWNER)
        await orchestrator.execute(first_plan.plan_id, OWNER)
        first_report = self.reports.get_by_source_job(self.project_id, OWNER, "job-validation-r1")
        revision_two = self._persist_revision_two()
        request = build_validation_request(
            revision_two,
            job_id="job-validation-r2",
            correlation_id="corr-revalidation",
            revalidation_of_report_id=first_report.report_id,
        )
        second_plan = orchestrator.create_plan([request], OWNER)

        completed = await orchestrator.execute(second_plan.plan_id, OWNER)
        second_report = self.reports.get_by_source_job(self.project_id, OWNER, "job-validation-r2")
        unchanged_first = self.reports.get(first_report.report_id, OWNER)

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(2, second_report.project_revision)
        self.assertEqual(first_report.report_id, second_report.revalidation_of_report_id)
        self.assertEqual(1, unchanged_first.project_revision)
        self.assertNotEqual(first_report.report_id, second_report.report_id)

    async def test_unsupported_requested_check_is_explicitly_skipped(self) -> None:
        worker = ValidationWorker(self.state, self.reports)
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([
            self._request(payload={"requested_checks": ["thermal-vacuum"]})
        ], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        report = self.reports.get_by_source_job(self.project_id, OWNER, "job-validation-r1")

        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(1, report.summary.skipped)
        self.assertEqual(ValidationCheckStatus.SKIPPED, report.findings[0].status)
        self.assertIn("not supported", report.findings[0].evidence[0])

    async def test_engine_failure_is_structured_and_retryable(self) -> None:
        worker = ValidationWorker(self.state, self.reports, CountingValidationEngine(fail=True))
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([self._request()], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        result = completed.jobs["job-validation-r1"].result

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual("validation_failed", result.error.code)
        self.assertTrue(result.error.retryable)
        self.assertIn("timed out", result.error.message)


if __name__ == "__main__":
    unittest.main()
