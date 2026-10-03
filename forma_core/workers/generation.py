"""Generation worker that turns one frozen DesignBrief into canonical project revision 1."""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from forma_core.agents.pipeline import (
    AgentPipelineEvent,
    PipelineCancelledError,
    emit_agent_pipeline_event,
    list_agent_pipeline_steps,
    observe_agent_pipeline,
)
from forma_core.workers.contracts import (
    WorkerArtifact,
    WorkerError,
    WorkerProgress,
    WorkerRequest,
    WorkerResult,
    WorkerResultStatus,
)
from forma_core.workers.registry import WorkerCapability, WorkerDefinition
from forma_core.workspaces.design_briefs import DesignBrief, prompt_safe_design_brief, uploaded_image_payload
from forma_core.workspaces.projects import (
    ProjectArtifact,
    ProjectRevision,
    ProjectRevisionOutcome,
    ProjectRevisionDraft,
    ProjectStateError,
    ProjectStateService,
    ProjectSystem,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.cad_generation import (
    cad_project_artifact,
)
from forma_core.workspaces.projects.output import attach_hardware_reference_image, attach_product_image
from forma_core.user_integrations import (
    ResolvedIntegrationSettings,
    UserIntegrationStore,
    resolve_user_integration_settings,
)


logger = logging.getLogger(__name__)


GENERATION_WORKER_ID = "generation-worker"
GENERATION_CAPABILITY_ID = "project.generate"
GENERATION_INPUT_VERSION = "design-brief.v1"
GENERATION_OUTPUT_VERSION = "project-revision.v1"


class GenerationWorkerPayload(BaseModel):
    """The Generation worker accepts no conversational input outside the frozen brief."""

    model_config = ConfigDict(extra="forbid")

    design_brief: DesignBrief


class GenerationEngine(Protocol):
    def generate(self, design_brief: DesignBrief) -> ProjectRevisionDraft | Awaitable[ProjectRevisionDraft]: ...


class HardwareIntermediateRepresentationGenerationEngine:
    """Adapter for the existing structured pipeline with legacy persistence disabled."""

    def __init__(
        self,
        *,
        provider_name: str | None = None,
        model_name: str | None = None,
        use_simulation: bool = False,
        generate_image: bool = True,
        settings: ResolvedIntegrationSettings | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.use_simulation = use_simulation
        self.generate_image = generate_image
        self.settings = settings

    def generate(
        self,
        design_brief: DesignBrief,
        *,
        generation_metadata: dict[str, Any] | None = None,
    ) -> ProjectRevisionDraft:
        from forma_core.agents.orchestrator import HardwarePipelineOrchestrator

        prompt = self._prompt(design_brief)
        image_data, image_media_type = uploaded_image_payload(design_brief)
        image_bytes, decoded_media_type = _reference_image_bytes(image_data)
        orchestrator = HardwarePipelineOrchestrator(
            use_simulation=self.use_simulation,
            provider_name=self.provider_name,
            model_name=self.model_name,
            persist_project=False,
            settings=self.settings,
        )
        state = orchestrator.generate_project(
            prompt,
            image_bytes=image_bytes,
            image_mime_type=decoded_media_type or image_media_type,
            generation_metadata={
                "project_id": str(design_brief.project_id),
                "design_brief_id": str(design_brief.design_brief_id),
                "design_brief_version": design_brief.brief_version,
                **(generation_metadata or {}),
            },
        )
        generation_error = (state.assembly_metadata or {}).get("generation_error")
        generation_run = (state.assembly_metadata or {}).get("generation_run")
        if (generation_error or (state.assembly_metadata or {}).get("status") == "failed") and not isinstance(
            generation_run,
            dict,
        ):
            message = generation_error.get("message") if isinstance(generation_error, dict) else None
            raise RuntimeError(str(message or "Structured hardware generation failed."))
        generation_status = str((state.assembly_metadata or {}).get("generation_status") or "succeeded")
        generate_product_image = self.generate_image and generation_status == "succeeded"
        if generate_product_image:
            emit_agent_pipeline_event("default", "image_generation", "started")
        attach_product_image(prompt, state, generate_image=generate_product_image, settings=self.settings)
        if image_data:
            attach_hardware_reference_image(state, image_data, media_type=decoded_media_type or image_media_type)
        if generate_product_image:
            image_status = (state.assembly_metadata or {}).get("image_output_status")
            emit_agent_pipeline_event(
                "default",
                "image_generation",
                "completed" if image_status == "succeeded" else "failed",
                details={
                    "image_output_status": image_status,
                    "provider": (state.assembly_metadata or {}).get("image_output_provider"),
                    "model": (state.assembly_metadata or {}).get("image_output_model"),
                },
            )
        return build_generation_draft(design_brief, state)

    @staticmethod
    def _prompt(design_brief: DesignBrief) -> str:
        return (
            "Generate an initial structured hardware project using only this frozen DesignBrief. "
            "Treat only the references declared inside the brief as reference inputs. Do not infer context "
            "from prior conversation or fetch undeclared sources.\n\n"
            f"Frozen DesignBrief:\n{prompt_safe_design_brief(design_brief).model_dump_json(indent=2)}"
        )


def _reference_image_bytes(image_data: str | None) -> tuple[bytes | None, str | None]:
    if not image_data:
        return None, None
    try:
        from forma_core.image_providers import _image_bytes_from_data

        return _image_bytes_from_data(image_data)
    except Exception:
        logger.warning("Could not decode the uploaded hardware reference image for generation.")
        return None, None


def build_generation_draft(design_brief: DesignBrief, state: HardwareIntermediateRepresentation) -> ProjectRevisionDraft:
    component_refs = [component.ref_des for component in state.components]
    known_component_refs = set(component_refs)
    used_system_ids = {"system-primary"}

    def unique_system_id(candidate: str) -> str:
        if candidate not in used_system_ids:
            used_system_ids.add(candidate)
            return candidate
        suffix = 2
        while f"{candidate}-{suffix}" in used_system_ids:
            suffix += 1
        resolved = f"{candidate}-{suffix}"
        used_system_ids.add(resolved)
        return resolved

    systems = [
        ProjectSystem(
            system_id="system-primary",
            kind="hardware",
            name=(state.overview.title if state.overview else design_brief.summary),
            component_refs=component_refs,
        )
    ]
    for rail in state.power_rails:
        rail_refs = [rail.source_component] if rail.source_component in known_component_refs else []
        rail_metadata: dict[str, Any] = {
            "voltage": rail.voltage,
            "max_current_capacity_ma": rail.max_current_capacity_ma,
            "source_component": rail.source_component,
        }
        if not rail_refs:
            rail_metadata["unresolved_component_refs"] = [rail.source_component]
        systems.append(ProjectSystem(
            system_id=unique_system_id(f"power-{rail.rail_id}"),
            kind="power",
            name=rail.rail_id,
            component_refs=rail_refs,
            metadata=rail_metadata,
        ))
    for bus in state.buses:
        declared_bus_components = {
            pin.ref_des
            for net in state.nets
            if net.net_id in bus.nets
            for pin in net.pins
        }
        bus_components = sorted(declared_bus_components.intersection(known_component_refs))
        unresolved_bus_components = sorted(declared_bus_components.difference(known_component_refs))
        bus_metadata: dict[str, Any] = {"bus_type": bus.bus_type, "net_ids": list(bus.nets)}
        if unresolved_bus_components:
            bus_metadata["unresolved_component_refs"] = unresolved_bus_components
        systems.append(ProjectSystem(
            system_id=unique_system_id(f"bus-{bus.bus_id}"),
            kind="bus",
            name=bus.bus_id,
            component_refs=bus_components,
            metadata=bus_metadata,
        ))

    revision_base = f"forma://projects/{design_brief.project_id}/revisions/1"
    artifacts = [
        ProjectArtifact(
            artifact_id="project-state",
            kind="project-state",
            uri=f"{revision_base}/state",
            media_type="application/vnd.forma.hardware-ir+json",
        )
    ]
    if state.components:
        artifacts.append(ProjectArtifact(
            artifact_id="bill-of-materials",
            kind="bom",
            uri=f"{revision_base}/bom",
            media_type="application/json",
        ))
    if state.nets:
        artifacts.append(ProjectArtifact(
            artifact_id="wiring-netlist",
            kind="wiring",
            uri=f"{revision_base}/wiring",
            media_type="application/json",
        ))
    if state.mechanical is not None:
        artifacts.append(ProjectArtifact(
            artifact_id="mechanical-plan",
            kind="mechanical",
            uri=f"{revision_base}/mechanical",
            media_type="application/json",
        ))
    if state.assembly:
        artifacts.append(ProjectArtifact(
            artifact_id="assembly-guide",
            kind="assembly",
            uri=f"{revision_base}/assembly",
            media_type="application/json",
        ))
    cad_artifact = cad_project_artifact(state, str(design_brief.project_id))
    if cad_artifact is not None:
        artifacts.append(cad_artifact)

    generation_run = (state.assembly_metadata or {}).get("generation_run") or {}
    generation_records = generation_run.get("records") if isinstance(generation_run, dict) else {}
    generation_records = generation_records if isinstance(generation_records, dict) else {}
    known_artifact_ids = {artifact.artifact_id for artifact in artifacts}
    for record in generation_records.values():
        artifact_payload = record.get("artifact") if isinstance(record, dict) else None
        if not isinstance(artifact_payload, dict):
            continue
        try:
            stage_artifact = ProjectArtifact.model_validate(artifact_payload)
        except ValidationError:
            continue
        if stage_artifact.artifact_id not in known_artifact_ids:
            artifacts.append(stage_artifact)
            known_artifact_ids.add(stage_artifact.artifact_id)

    assumptions = list(dict.fromkeys([
        *design_brief.assumptions,
        "Generated component selections and topology remain provisional until validation.",
    ]))
    return ProjectRevisionDraft(
        state=state,
        components=list(state.components),
        systems=systems,
        artifacts=artifacts,
        assumptions=assumptions,
    )


ProgressReporter = Callable[[WorkerProgress], Awaitable[None]]
ProjectPublisher = Callable[[ProjectRevision, DesignBrief, str], str]


class GenerationWorker:
    """Registered worker implementation for the `project.generate` capability."""

    def __init__(
        self,
        state_service: ProjectStateService,
        engine: GenerationEngine | None = None,
        project_publisher: ProjectPublisher | None = None,
    ) -> None:
        self._state = state_service
        self._engine = engine or HardwareIntermediateRepresentationGenerationEngine()
        self._project_publisher = project_publisher

    def worker_definition(self) -> WorkerDefinition:
        return WorkerDefinition(
            worker_id=GENERATION_WORKER_ID,
            name="Forma Generation Worker",
            worker_version="1.0.0",
            capabilities=[
                WorkerCapability(
                    capability_id=GENERATION_CAPABILITY_ID,
                    description="Create initial canonical project state from a frozen DesignBrief.",
                    supported_input_versions=[GENERATION_INPUT_VERSION],
                    supported_output_versions=[GENERATION_OUTPUT_VERSION],
                )
            ],
        )

    async def execute(self, request: WorkerRequest, report_progress: ProgressReporter) -> WorkerResult:
        context = _worker_context(request)
        try:
            payload = GenerationWorkerPayload.model_validate(request.payload)
            _validate_request_identity(request, payload.design_brief)
            owner_user_id = str(request.metadata.get("execution_owner_user_id") or "").strip()
            if not owner_user_id:
                raise ProjectStateError(
                    "generation_owner_missing",
                    "Generation execution is missing its owner scope.",
                )
            payload.design_brief = self._state.require_frozen_design_brief(payload.design_brief, owner_user_id)
        except ValidationError as exc:
            return _failure_result(
                request,
                ProjectStateError("invalid_generation_request", "Generation payload is invalid.", context={
                    "validation_errors": exc.errors(include_url=False),
                }),
                retryable=False,
            )
        except (ProjectStateError, ValueError) as exc:
            return _failure_result(request, exc, retryable=False)

        execution_attempt = max(1, int(request.metadata.get("execution_attempt") or 1))
        source_job_id = request.job_id if execution_attempt == 1 else f"{request.job_id}:attempt:{execution_attempt}"
        replay = self._state.get_by_source_job(request.project_id, owner_user_id, source_job_id)
        if replay is not None:
            if (
                replay.design_brief_id != request.design_brief_id
                or replay.design_brief_version != request.design_brief_version
            ):
                return _failure_result(
                    request,
                    ProjectStateError(
                        "project_revision_idempotency_conflict",
                        "The source job is already attached to a different project revision or DesignBrief.",
                    ),
                    retryable=False,
                )
            try:
                self._publish(replay, payload.design_brief, owner_user_id)
            except Exception as exc:
                return _failure_result(request, exc, retryable=True)
            return _success_result(request, ProjectRevisionOutcome(revision=replay, idempotent_replay=True))

        cancellation_check = request.metadata.get("pipeline_cancellation_check")
        if not callable(cancellation_check):
            cancellation_check = None

        try:
            generation_engine = self._engine
            if isinstance(self._engine, HardwareIntermediateRepresentationGenerationEngine):
                settings = await asyncio.to_thread(
                    resolve_user_integration_settings,
                    UserIntegrationStore.for_user(owner_user_id),
                    fail_open=False,
                )
                generation_engine = copy.copy(self._engine)
                generation_engine.settings = settings
            if cancellation_check is not None and cancellation_check():
                return _cancelled_result(request)
            progress_sequence = max(0, int(request.metadata.get("progress_sequence_start") or 0)) + 1
            await report_progress(WorkerProgress(
                **context,
                sequence=progress_sequence,
                status="running",
                percent_complete=10,
                message="Generating structured project state from the frozen DesignBrief.",
            ))
            if isinstance(generation_engine, HardwareIntermediateRepresentationGenerationEngine):
                event_loop = asyncio.get_running_loop()

                prior_generation_run = request.metadata.get("prior_generation_run")
                prior_generation_run = prior_generation_run if isinstance(prior_generation_run, dict) else None
                if prior_generation_run is None:
                    try:
                        latest = self._state.get_latest(request.project_id, owner_user_id)
                    except ProjectStateError:
                        latest = None
                    candidate_run = (
                        ((latest.state.assembly_metadata or {}).get("generation_run") or {})
                        if latest is not None
                        else {}
                    )
                    prior_generation_run = candidate_run if isinstance(candidate_run, dict) and candidate_run else None
                retry_stage = str(request.metadata.get("retry_stage") or "").strip() or None

                def generate_with_observation() -> ProjectRevisionDraft:
                    def record_pipeline_event(event: AgentPipelineEvent) -> None:
                        nonlocal progress_sequence
                        if cancellation_check is not None and cancellation_check():
                            raise PipelineCancelledError("Agent pipeline was cancelled.")
                        progress_sequence += 1
                        future = asyncio.run_coroutine_threadsafe(
                            report_progress(WorkerProgress(
                                **context,
                                sequence=progress_sequence,
                                status="running",
                                percent_complete=_pipeline_event_percent(event),
                                message=f"{event.label}: {event.status.replace('_', ' ')}",
                                metadata={"pipeline_event": event.as_dict()},
                            )),
                            event_loop,
                        )
                        future.result()

                    def record_stage_checkpoint(checkpoint: dict[str, Any]) -> None:
                        nonlocal progress_sequence
                        if cancellation_check is not None and cancellation_check():
                            raise PipelineCancelledError("Agent pipeline was cancelled.")
                        progress_sequence += 1
                        future = asyncio.run_coroutine_threadsafe(
                            report_progress(WorkerProgress(
                                **context,
                                sequence=progress_sequence,
                                status="running",
                                percent_complete=None,
                                message="Persisting generation stage checkpoint.",
                                metadata={"generation_stage_checkpoint": checkpoint},
                            )),
                            event_loop,
                        )
                        future.result()

                    with observe_agent_pipeline(record_pipeline_event, cancellation_check=cancellation_check):
                        generation_parameters = inspect.signature(generation_engine.generate).parameters
                        if "generation_metadata" not in generation_parameters:
                            return generation_engine.generate(payload.design_brief)
                        return generation_engine.generate(
                            payload.design_brief,
                            generation_metadata={
                                "prior_generation_run": prior_generation_run,
                                "retry_stage": retry_stage,
                                "stage_checkpoint": record_stage_checkpoint,
                                "generation_mode": request.metadata.get("generation_mode", "regular"),
                                "visual_approval_policy": request.metadata.get("visual_approval_policy"),
                                "cad_required": bool(request.metadata.get("cad_required", False)),
                            },
                        )

                candidate = await asyncio.to_thread(generate_with_observation)
                if cancellation_check is not None and cancellation_check():
                    raise PipelineCancelledError("Agent pipeline was cancelled.")
            else:
                candidate = self._engine.generate(payload.design_brief)
                if hasattr(candidate, "__await__"):
                    candidate = await candidate
            draft = ProjectRevisionDraft.model_validate(candidate)
            progress_sequence += 1
            await report_progress(WorkerProgress(
                **context,
                sequence=progress_sequence,
                status="running",
                percent_complete=80,
                message="Persisting initial project revision.",
            ))
            try:
                self._state.get_latest(request.project_id, owner_user_id)
            except ProjectStateError:
                outcome = self._state.create_initial_revision(
                    draft,
                    project_id=request.project_id,
                    owner_user_id=owner_user_id,
                    design_brief_id=request.design_brief_id,
                    design_brief_version=request.design_brief_version,
                    source_job_id=source_job_id,
                )
            else:
                outcome = self._state.create_revision(
                    draft,
                    project_id=request.project_id,
                    owner_user_id=owner_user_id,
                    source_job_id=source_job_id,
                    design_brief_id=request.design_brief_id,
                    design_brief_version=request.design_brief_version,
                )
            self._publish(outcome.revision, payload.design_brief, owner_user_id)
        except PipelineCancelledError:
            return _cancelled_result(request)
        except ProjectStateError as exc:
            return _failure_result(request, exc, retryable=exc.retryable)
        except Exception as exc:
            return _failure_result(request, exc, retryable=True)

        return _success_result(request, outcome)

    def _publish(self, revision: ProjectRevision, brief: DesignBrief, owner_user_id: str) -> None:
        if self._project_publisher is not None:
            self._project_publisher(revision, brief, owner_user_id)


def _pipeline_event_percent(event: AgentPipelineEvent) -> float:
    steps = list_agent_pipeline_steps(event.workflow)
    index = next((index for index, step in enumerate(steps) if step.get("id") == event.step_id), 0)
    completed = index + (1 if event.status in {"completed", "skipped"} else 0)
    return min(75.0, max(10.0, 10.0 + (completed / max(len(steps), 1)) * 65.0))


def _cancelled_result(request: WorkerRequest) -> WorkerResult:
    context = _worker_context(request)
    error = WorkerError(
        **context,
        code="generation_cancelled",
        message="Build stopped by the user.",
        retryable=True,
    )
    return WorkerResult(
        **context,
        output_contract_version=GENERATION_OUTPUT_VERSION,
        status=WorkerResultStatus.CANCELLED,
        error=error,
    )


def _success_result(request: WorkerRequest, outcome: ProjectRevisionOutcome) -> WorkerResult:
    context = _worker_context(request)
    revision = outcome.revision
    artifacts = [
        WorkerArtifact(
            **context,
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            uri=artifact.uri,
            media_type=artifact.media_type,
            checksum=artifact.checksum,
            metadata={
                **artifact.metadata,
                "project_revision": revision.revision,
                "design_brief_version": revision.design_brief_version,
            },
        )
        for artifact in revision.artifacts
    ]
    generation_status = str((revision.state.assembly_metadata or {}).get("generation_status") or "succeeded")
    generation_error = None
    if generation_status in {"partial", "failed"}:
        generation_error = WorkerError(
            **context,
            code="generation_partial" if generation_status == "partial" else "generation_root_failed",
            message=(
                "One or more generation stages failed; successful artifacts were preserved."
                if generation_status == "partial"
                else "A required root generation stage failed; diagnostics were preserved."
            ),
            retryable=True,
            details={
                "stage_failures": (revision.state.assembly_metadata or {}).get("generation_stage_failures") or [],
            },
        )
    result_status = (
        WorkerResultStatus.PARTIAL
        if generation_status == "partial"
        else WorkerResultStatus.FAILED
        if generation_status == "failed"
        else WorkerResultStatus.SUCCEEDED
    )
    retry_metadata = _generation_retry_metadata(revision.state) if generation_error is not None else None
    return WorkerResult(
        **context,
        output_contract_version=GENERATION_OUTPUT_VERSION,
        status=result_status,
        output={
            "project_revision": revision.model_dump(mode="json"),
            "components": [item.model_dump(mode="json") for item in revision.components],
            "systems": [item.model_dump(mode="json") for item in revision.systems],
            "artifacts": [item.model_dump(mode="json") for item in revision.artifacts],
            "assumptions": list(revision.assumptions),
            "idempotent_replay": outcome.idempotent_replay,
        },
        artifacts=artifacts,
        error=generation_error,
        metadata={"generation_retry": retry_metadata} if retry_metadata is not None else {},
    )


def _generation_retry_metadata(state: HardwareIntermediateRepresentation) -> dict[str, Any] | None:
    generation_run = (state.assembly_metadata or {}).get("generation_run") or {}
    records = generation_run.get("records") if isinstance(generation_run, dict) else None
    if not isinstance(records, dict):
        return None
    retry_stage = next((
        stage_id
        for stage_id, record in records.items()
        if isinstance(record, dict) and record.get("status") == "failed"
    ), None)
    if not retry_stage:
        return None
    invalidated = {retry_stage}
    changed = True
    while changed:
        changed = False
        for stage_id, record in records.items():
            dependencies = record.get("dependencies") if isinstance(record, dict) else []
            if stage_id not in invalidated and any(dep in invalidated for dep in (dependencies or [])):
                invalidated.add(stage_id)
                changed = True
    if retry_stage != "package_project" and "package_project" in records:
        invalidated.add("package_project")
    return {
        "retry_stage": retry_stage,
        "prior_generation_run": generation_run,
        "invalidated_stage_ids": sorted(invalidated),
    }


def _validate_request_identity(request: WorkerRequest, brief: DesignBrief) -> None:
    if (
        brief.project_id != request.project_id
        or brief.design_brief_id != request.design_brief_id
        or brief.brief_version != request.design_brief_version
        or request.project_revision != 1
    ):
        raise ProjectStateError(
            "generation_context_mismatch",
            "Generation request identity does not match its frozen DesignBrief or initial revision.",
            context={
                "request_project_id": str(request.project_id),
                "brief_project_id": str(brief.project_id),
                "request_design_brief_id": str(request.design_brief_id),
                "brief_design_brief_id": str(brief.design_brief_id),
                "request_design_brief_version": request.design_brief_version,
                "brief_design_brief_version": brief.brief_version,
                "request_project_revision": request.project_revision,
            },
        )


def _worker_context(request: WorkerRequest) -> dict[str, Any]:
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


def _failure_result(request: WorkerRequest, exc: Exception, *, retryable: bool) -> WorkerResult:
    context = _worker_context(request)
    code = str(getattr(exc, "code", "generation_failed"))
    message = str(getattr(exc, "message", "") or str(exc) or exc.__class__.__name__)
    details = {
        "exception_type": exc.__class__.__name__,
        "context": dict(getattr(exc, "context", {}) or {}),
    }
    error = WorkerError(
        **context,
        code=code,
        message=message,
        retryable=retryable,
        details=details,
    )
    return WorkerResult(
        **context,
        output_contract_version=GENERATION_OUTPUT_VERSION,
        status=WorkerResultStatus.FAILED,
        error=error,
    )


__all__ = [
    "GENERATION_CAPABILITY_ID",
    "GENERATION_INPUT_VERSION",
    "GENERATION_OUTPUT_VERSION",
    "GENERATION_WORKER_ID",
    "GenerationEngine",
    "GenerationWorker",
    "GenerationWorkerPayload",
    "HardwareIntermediateRepresentationGenerationEngine",
    "build_generation_draft",
]
