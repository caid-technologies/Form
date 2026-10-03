from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository
from forma_core.workers import (
    WORKER_CONTRACT_VERSION,
    OrchestrationTaskStatus,
    WorkerCapability,
    WorkerDefinition,
    WorkerDependency,
    WorkerOrchestrator,
    WorkerPlanStatus,
    WorkerPlanningError,
    WorkerProgress,
    WorkerRequest,
    WorkerResult,
)
from forma_core.workspaces.workflow import (
    ProjectWorkflowService,
    ProjectWorkflowState,
    WorkflowActorType,
)


OWNER = "orchestrator-user"
PROJECT_ID = uuid.UUID("a296d80f-99da-4db5-9f95-3d062b553d30")
BRIEF_ID = uuid.UUID("ecaa71e8-f1c5-4a94-bf21-a08128cddeca")


def request_context(worker_id: str, capability_id: str, job_id: str) -> dict[str, Any]:
    return {
        "contract_version": WORKER_CONTRACT_VERSION,
        "project_id": PROJECT_ID,
        "project_revision": 4,
        "design_brief_id": BRIEF_ID,
        "design_brief_version": 2,
        "job_id": job_id,
        "correlation_id": "corr_orchestration_test",
        "worker_id": worker_id,
        "capability_id": capability_id,
    }


def make_request(
    worker_id: str,
    job_id: str,
    *,
    dependencies: list[WorkerDependency] | None = None,
) -> WorkerRequest:
    return WorkerRequest(
        **request_context(worker_id, f"{worker_id}.run", job_id),
        input_contract_version="design-brief.v1",
        dependencies=dependencies or [],
        payload={"job": job_id},
    )


class FakeWorker:
    def __init__(
        self,
        worker_id: str,
        *,
        delay: float = 0.0,
        events: list[str] | None = None,
        tracker: dict[str, int] | None = None,
        fail: bool = False,
    ) -> None:
        self.worker_id = worker_id
        self.delay = delay
        self.events = events if events is not None else []
        self.tracker = tracker
        self.fail = fail
        self.execution_count = 0
        self.received: list[WorkerRequest] = []

    def worker_definition(self) -> WorkerDefinition:
        return WorkerDefinition(
            worker_id=self.worker_id,
            name=f"Fake {self.worker_id}",
            worker_version="1.0",
            capabilities=[
                WorkerCapability(
                    capability_id=f"{self.worker_id}.run",
                    description="Exercise orchestration behavior.",
                    supported_input_versions=["design-brief.v1"],
                    supported_output_versions=["worker-output.v1"],
                )
            ],
        )

    async def execute(self, request: WorkerRequest, report_progress: Any) -> WorkerResult:
        self.execution_count += 1
        self.received.append(request)
        self.events.append(f"start:{request.job_id}")
        if self.tracker is not None:
            self.tracker["active"] += 1
            self.tracker["maximum"] = max(self.tracker["maximum"], self.tracker["active"])
        await report_progress(
            WorkerProgress(
                **request_context(request.worker_id, request.capability_id, request.job_id),
                sequence=1,
                status="running",
                percent_complete=50,
                message="Fake worker is running.",
            )
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.tracker is not None:
            self.tracker["active"] -= 1
        if self.fail:
            raise RuntimeError(f"{self.worker_id} failed")
        self.events.append(f"end:{request.job_id}")
        return WorkerResult(
            **request_context(request.worker_id, request.capability_id, request.job_id),
            output_contract_version="worker-output.v1",
            status="succeeded",
            output={"completed": request.job_id},
        )


class CheckpointThenFailWorker(FakeWorker):
    async def execute(self, request: WorkerRequest, report_progress: Any) -> WorkerResult:
        self.execution_count += 1
        self.received.append(request)
        sequence = int(request.metadata.get("progress_sequence_start") or 0)
        sequence += 1
        await report_progress(WorkerProgress(
            **request_context(request.worker_id, request.capability_id, request.job_id),
            sequence=sequence,
            status="running",
            percent_complete=75,
            message="Packaging project artifacts: completed",
            metadata={"pipeline_event": {
                "workflow": "default",
                "step_id": "package_project",
                "status": "completed",
                "agent": "ArtifactPackager",
                "label": "Packaging project artifacts",
                "description": "Package the generated project.",
                "observed_at": "2026-08-16T00:00:00Z",
                "details": {},
            }},
        ))
        sequence += 1
        await report_progress(WorkerProgress(
            **request_context(request.worker_id, request.capability_id, request.job_id),
            sequence=sequence,
            status="running",
            percent_complete=None,
            message="Persisting generation stage checkpoint.",
            metadata={"generation_stage_checkpoint": {
                "generation_run_id": "run-finalization-failure",
                "workflow": "default",
                "retry_stage": None,
                "status": "succeeded",
                "record": {
                    "stage_id": "package_project",
                    "status": "succeeded",
                    "dependencies": [],
                },
            }},
        ))
        raise RuntimeError("revision finalization failed")


class WorkerOrchestratorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.provider = create_sqlite_provider(
            source="worker orchestrator test",
            url=f"sqlite:///{Path(self.directory.name) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        self.provider.initialize()
        self.repository = SqlAlchemyRepository(self.provider.session_factory)
        self.workflow = ProjectWorkflowService(self.repository)

    def tearDown(self) -> None:
        self.provider.dispose()
        self.directory.cleanup()

    def enter_building(self) -> None:
        self.workflow.initialize(str(PROJECT_ID), OWNER)
        self.workflow.transition(
            str(PROJECT_ID),
            OWNER,
            ProjectWorkflowState.BUILDING,
            actor_type=WorkflowActorType.USER,
            actor_id=OWNER,
            reason="Begin test build.",
        )

    async def test_independent_jobs_overlap_and_terminal_plan_advances_workflow(self) -> None:
        self.enter_building()
        tracker = {"active": 0, "maximum": 0}
        electrical = FakeWorker("electrical", delay=0.04, tracker=tracker)
        mechanical = FakeWorker("mechanical", delay=0.04, tracker=tracker)
        orchestrator = WorkerOrchestrator(
            self.repository,
            [electrical, mechanical],
            workflow_service=self.workflow,
        )
        plan = orchestrator.create_plan(
            [make_request("electrical", "job-electrical"), make_request("mechanical", "job-mechanical")],
            OWNER,
            max_concurrency=2,
        )

        completed = await orchestrator.execute(plan.plan_id, OWNER)
        persisted = orchestrator.get_plan(plan.plan_id, OWNER)
        workflow = self.workflow.get(str(PROJECT_ID), OWNER)

        self.assertEqual(2, tracker["maximum"])
        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(completed, persisted)
        self.assertEqual({"job-electrical", "job-mechanical"}, set(completed.aggregate))
        self.assertEqual(ProjectWorkflowState.AWAITING_FEEDBACK, workflow.state)
        self.assertEqual(1, len(completed.jobs["job-electrical"].progress))

    async def test_dependency_waits_for_success_and_receives_only_persisted_worker_results(self) -> None:
        self.enter_building()
        events: list[str] = []
        source = FakeWorker("source", delay=0.02, events=events)
        dependent = FakeWorker("dependent", events=events)
        orchestrator = WorkerOrchestrator(self.repository, [source, dependent], workflow_service=self.workflow)
        source_request = make_request("source", "job-source")
        dependent_request = make_request(
            "dependent",
            "job-dependent",
            dependencies=[WorkerDependency(job_id="job-source", required=True)],
        )
        plan = orchestrator.create_plan([source_request, dependent_request], OWNER)

        completed = await orchestrator.execute(plan.plan_id, OWNER)

        self.assertLess(events.index("end:job-source"), events.index("start:job-dependent"))
        dependency_results = dependent.received[0].payload["dependency_results"]
        self.assertEqual("succeeded", dependency_results["job-source"]["status"])
        self.assertNotIn("conversation", dependent.received[0].payload)
        self.assertEqual(OrchestrationTaskStatus.SUCCEEDED, completed.jobs["job-dependent"].status)

    async def test_failed_required_dependency_blocks_dependent_and_persists_error(self) -> None:
        self.enter_building()
        failing = FakeWorker("failing", fail=True)
        dependent = FakeWorker("dependent")
        orchestrator = WorkerOrchestrator(self.repository, [failing, dependent], workflow_service=self.workflow)
        plan = orchestrator.create_plan(
            [
                make_request("failing", "job-failing"),
                make_request(
                    "dependent",
                    "job-dependent",
                    dependencies=[WorkerDependency(job_id="job-failing", required=True)],
                ),
            ],
            OWNER,
        )

        completed = await orchestrator.execute(plan.plan_id, OWNER)

        self.assertEqual(WorkerPlanStatus.FAILED, completed.status)
        self.assertEqual(OrchestrationTaskStatus.FAILED, completed.jobs["job-failing"].status)
        self.assertEqual("worker_execution_failed", completed.jobs["job-failing"].error.code)
        self.assertEqual(OrchestrationTaskStatus.BLOCKED, completed.jobs["job-dependent"].status)
        self.assertEqual("required_dependency_failed", completed.jobs["job-dependent"].error.code)
        self.assertEqual(0, dependent.execution_count)

    async def test_failed_plan_can_be_reset_and_run_again(self) -> None:
        self.enter_building()
        source = FakeWorker("source")
        flaky = FakeWorker("flaky", fail=True)
        orchestrator = WorkerOrchestrator(self.repository, [source, flaky], workflow_service=self.workflow)
        plan = orchestrator.create_plan(
            [
                make_request("source", "job-source"),
                make_request(
                    "flaky",
                    "job-flaky",
                    dependencies=[WorkerDependency(job_id="job-source", required=True)],
                ),
            ],
            OWNER,
        )

        failed = await orchestrator.execute(plan.plan_id, OWNER)
        reset = await orchestrator.reset(plan.plan_id, OWNER)

        self.assertEqual(WorkerPlanStatus.FAILED, failed.status)
        self.assertEqual(2, reset.attempt)
        self.assertEqual(WorkerPlanStatus.PLANNED, reset.status)
        self.assertEqual(OrchestrationTaskStatus.SUCCEEDED, reset.jobs["job-source"].status)
        self.assertEqual(OrchestrationTaskStatus.QUEUED, reset.jobs["job-flaky"].status)
        self.assertEqual([], reset.jobs["job-flaky"].progress)
        self.assertIsNone(reset.jobs["job-flaky"].error)
        self.assertEqual(ProjectWorkflowState.BUILDING, self.workflow.get(str(PROJECT_ID), OWNER).state)

        flaky.fail = False
        completed = await orchestrator.execute(plan.plan_id, OWNER)

        self.assertEqual(1, source.execution_count)
        self.assertEqual(2, flaky.execution_count)
        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)
        self.assertEqual(ProjectWorkflowState.AWAITING_FEEDBACK, self.workflow.get(str(PROJECT_ID), OWNER).state)

    async def test_non_failed_plan_cannot_be_reset(self) -> None:
        self.enter_building()
        worker = FakeWorker("generation")
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([make_request("generation", "job-generation")], OWNER)

        with self.assertRaises(WorkerPlanningError) as raised:
            await orchestrator.reset(plan.plan_id, OWNER)

        self.assertEqual("worker_plan_not_failed", raised.exception.code)

    async def test_finalization_failure_reset_reuses_completed_generation_checkpoints(self) -> None:
        self.enter_building()
        worker = CheckpointThenFailWorker("generation")
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([make_request("generation", "job-generation")], OWNER)

        failed = await orchestrator.execute(plan.plan_id, OWNER)
        reset = await orchestrator.reset(plan.plan_id, OWNER)

        self.assertEqual(WorkerPlanStatus.FAILED, failed.status)
        self.assertEqual(2, len(reset.jobs["job-generation"].progress))
        self.assertEqual(
            "run-finalization-failure",
            reset.jobs["job-generation"].request.metadata["prior_generation_run"]["generation_run_id"],
        )
        self.assertIsNone(reset.jobs["job-generation"].request.metadata["retry_stage"])

    async def test_named_job_retry_preserves_successful_upstream_results(self) -> None:
        self.enter_building()
        source = FakeWorker("source")
        flaky = FakeWorker("flaky", fail=True)
        downstream = FakeWorker("downstream")
        orchestrator = WorkerOrchestrator(
            self.repository,
            [source, flaky, downstream],
            workflow_service=self.workflow,
        )
        plan = orchestrator.create_plan(
            [
                make_request("source", "job-source"),
                make_request(
                    "flaky",
                    "job-flaky",
                    dependencies=[WorkerDependency(job_id="job-source", required=True)],
                ),
                make_request(
                    "downstream",
                    "job-downstream",
                    dependencies=[WorkerDependency(job_id="job-flaky", required=True)],
                ),
            ],
            OWNER,
        )
        await orchestrator.execute(plan.plan_id, OWNER)

        reset = await orchestrator.reset_job(plan.plan_id, OWNER, "job-flaky")

        self.assertEqual(OrchestrationTaskStatus.SUCCEEDED, reset.jobs["job-source"].status)
        self.assertEqual(OrchestrationTaskStatus.QUEUED, reset.jobs["job-flaky"].status)
        self.assertEqual(OrchestrationTaskStatus.QUEUED, reset.jobs["job-downstream"].status)
        flaky.fail = False
        completed = await orchestrator.execute(plan.plan_id, OWNER)
        self.assertEqual(1, source.execution_count)
        self.assertEqual(2, flaky.execution_count)
        self.assertEqual(1, downstream.execution_count)
        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)

    async def test_planned_build_can_be_cancelled_without_running_workers(self) -> None:
        self.enter_building()
        worker = FakeWorker("generation")
        orchestrator = WorkerOrchestrator(self.repository, [worker], workflow_service=self.workflow)
        plan = orchestrator.create_plan([make_request("generation", "job-generation")], OWNER)

        cancelled = await orchestrator.cancel(plan.plan_id, OWNER)
        replay = await orchestrator.cancel(plan.plan_id, OWNER)

        self.assertEqual(WorkerPlanStatus.CANCELLED, cancelled.status)
        self.assertEqual(OrchestrationTaskStatus.CANCELLED, cancelled.jobs["job-generation"].status)
        self.assertEqual("worker_cancelled", cancelled.jobs["job-generation"].error.code)
        self.assertEqual(cancelled, replay)
        self.assertEqual(0, worker.execution_count)
        self.assertEqual(ProjectWorkflowState.AWAITING_FEEDBACK, self.workflow.get(str(PROJECT_ID), OWNER).state)

    async def test_persisted_plan_survives_orchestrator_restart_without_rerunning_success(self) -> None:
        self.enter_building()
        first = FakeWorker("first")
        second = FakeWorker("second")
        orchestrator = WorkerOrchestrator(self.repository, [first, second], workflow_service=self.workflow)
        plan = orchestrator.create_plan(
            [
                make_request("first", "job-first"),
                make_request(
                    "second",
                    "job-second",
                    dependencies=[WorkerDependency(job_id="job-first")],
                ),
            ],
            OWNER,
        )
        first_request = plan.jobs["job-first"].request
        first_result = WorkerResult(
            **request_context("first", "first.run", "job-first"),
            output_contract_version="worker-output.v1",
            status="succeeded",
            output={"completed": "job-first"},
        )
        plan.jobs["job-first"].status = OrchestrationTaskStatus.SUCCEEDED
        plan.jobs["job-first"].result = first_result
        plan.jobs["job-first"].completed_at = first_result.completed_at
        plan.jobs["job-second"].status = OrchestrationTaskStatus.RUNNING
        self.repository.update_worker_execution_plan(
            plan.plan_id,
            OWNER,
            {"status": "running", "state_json": plan.model_dump(mode="json")},
        )

        restarted_first = FakeWorker("first")
        restarted_second = FakeWorker("second")
        restarted = WorkerOrchestrator(
            self.repository,
            [restarted_first, restarted_second],
            workflow_service=ProjectWorkflowService(self.repository),
        )
        restored = restarted.get_plan(plan.plan_id, OWNER)
        completed = await restarted.execute(plan.plan_id, OWNER)

        self.assertEqual(first_request, restored.jobs["job-first"].request)
        self.assertEqual(OrchestrationTaskStatus.SUCCEEDED, restored.jobs["job-first"].status)
        self.assertEqual(0, restarted_first.execution_count)
        self.assertEqual(1, restarted_second.execution_count)
        self.assertEqual(WorkerPlanStatus.SUCCEEDED, completed.status)

    async def test_missing_dependencies_and_cycles_fail_before_execution(self) -> None:
        first = FakeWorker("first")
        second = FakeWorker("second")
        orchestrator = WorkerOrchestrator(self.repository, [first, second], workflow_service=self.workflow)

        with self.assertRaises(WorkerPlanningError) as missing:
            orchestrator.create_plan(
                [
                    make_request(
                        "first",
                        "job-first",
                        dependencies=[WorkerDependency(job_id="job-missing")],
                    )
                ],
                OWNER,
            )

        with self.assertRaises(WorkerPlanningError) as cyclic:
            orchestrator.create_plan(
                [
                    make_request(
                        "first",
                        "job-first",
                        dependencies=[WorkerDependency(job_id="job-second")],
                    ),
                    make_request(
                        "second",
                        "job-second",
                        dependencies=[WorkerDependency(job_id="job-first")],
                    ),
                ],
                OWNER,
            )

        self.assertEqual("missing_worker_dependency", missing.exception.code)
        self.assertEqual("cyclic_worker_dependencies", cyclic.exception.code)
        self.assertEqual(0, first.execution_count)
        self.assertEqual(0, second.execution_count)


if __name__ == "__main__":
    unittest.main()
