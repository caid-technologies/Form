from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Iterator, List, Mapping, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forma_core.jobs.source_usage import DEFAULT_WORKFLOW_ID, WEB_RESEARCH_WORKFLOW_ID, normalize_generation_workflow_id
from forma_core.workspaces.projects.design_lifecycle import (
    FidelityLevel,
    OperationCost,
    stage_cost,
    stage_fidelity,
)
from forma_core.workspaces.projects.state import ProjectArtifact


@dataclass(frozen=True)
class AgentPipelineStep:
    id: str
    agent: str
    label: str
    description: str
    duration_ms: int = 5500
    optional: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentPipelineEvent:
    workflow: str
    step_id: str
    status: str
    agent: str
    label: str
    description: str
    observed_at: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


PipelineEventCallback = Callable[[AgentPipelineEvent], None]
PipelineCancellationCheck = Callable[[], bool]


class GenerationStageStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class GenerationStageSpec(BaseModel):
    """Dependency, fidelity, and resource contract for one generation stage."""

    model_config = ConfigDict(extra="forbid")

    stage_id: str
    dependencies: List[str] = Field(default_factory=list)
    fidelity: Optional[FidelityLevel] = None
    cost: Optional[OperationCost] = None
    approval_gate: bool = False

    @model_validator(mode="after")
    def populate_planner_metadata(self) -> "GenerationStageSpec":
        if self.fidelity is None:
            self.fidelity = stage_fidelity(self.stage_id)
        if self.cost is None:
            self.cost = stage_cost(self.stage_id)
        return self


class GenerationStageRecord(BaseModel):
    """Persistable lifecycle and artifact payload for one generation stage."""

    model_config = ConfigDict(extra="forbid")

    stage_id: str
    status: GenerationStageStatus = GenerationStageStatus.NOT_STARTED
    dependencies: List[str] = Field(default_factory=list)
    fidelity: FidelityLevel = FidelityLevel.LOGICAL
    cost: OperationCost = Field(default_factory=OperationCost)
    approval_gate: bool = False
    input_artifact_ids: List[str] = Field(default_factory=list)
    input_fingerprint: Optional[str] = None
    output_fingerprint: Optional[str] = None
    artifact_id: Optional[str] = None
    artifact: Optional[ProjectArtifact] = None
    output: Any = None
    attempt: int = Field(default=0, ge=0)
    error: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    attempt_history: List[dict[str, Any]] = Field(default_factory=list)


GenerationStagePersistence = Callable[["GenerationStageRun", Optional[GenerationStageRecord]], None]


class GenerationStageRun:
    """Dependency-aware stage runner that checkpoints every lifecycle transition.

    Stage records now expose fidelity/cost metadata and content fingerprints so
    callers can reason about the least expensive useful operation and reuse
    completed work when dependency artifacts have not changed.
    """

    def __init__(
        self,
        workflow: str,
        specs: List[GenerationStageSpec],
        *,
        run_id: Optional[str] = None,
        prior_records: Optional[Mapping[str, Any]] = None,
        retry_stage: Optional[str] = None,
        replay_retry: bool = False,
        persist: Optional[GenerationStagePersistence] = None,
    ) -> None:
        self.workflow = pipeline_workflow_id(workflow)
        self.run_id = str(run_id or f"generation_{uuid4().hex}")
        self.specs = {spec.stage_id: spec for spec in specs}
        self.persist = persist
        self.retry_stage = str(retry_stage or "").strip() or None
        if self.retry_stage and self.retry_stage not in self.specs:
            raise ValueError(f"Unknown generation stage '{self.retry_stage}'.")
        self.records: dict[str, GenerationStageRecord] = {}
        for stage_id, spec in self.specs.items():
            raw = (prior_records or {}).get(stage_id)
            if raw:
                record = GenerationStageRecord.model_validate(raw)
                record.dependencies = list(spec.dependencies)
                record.fidelity = spec.fidelity or stage_fidelity(stage_id)
                record.cost = spec.cost or stage_cost(stage_id)
                record.approval_gate = spec.approval_gate
                self.records[stage_id] = record
            else:
                self.records[stage_id] = GenerationStageRecord(
                    stage_id=stage_id,
                    dependencies=list(spec.dependencies),
                    fidelity=spec.fidelity or stage_fidelity(stage_id),
                    cost=spec.cost or stage_cost(stage_id),
                    approval_gate=spec.approval_gate,
                )
        for record in self.records.values():
            if record.status == GenerationStageStatus.RUNNING:
                record.status = GenerationStageStatus.FAILED
                record.error = {
                    "code": "generation_stage_interrupted",
                    "message": "The generation process ended before this stage completed.",
                }
                record.completed_at = _utc_now()
                self._archive_attempt(record)
        self.invalidated = self._retry_closure(self.retry_stage) if self.retry_stage and not replay_retry else set()
        self._clear_records(self.invalidated)
        self._invalidate_fingerprint_mismatches()

    def _retry_closure(self, stage_id: Optional[str]) -> set[str]:
        if not stage_id:
            return set()
        invalidated = {stage_id}
        changed = True
        while changed:
            changed = False
            for candidate, spec in self.specs.items():
                if candidate not in invalidated and any(dep in invalidated for dep in spec.dependencies):
                    invalidated.add(candidate)
                    changed = True
        if stage_id != "package_project" and "package_project" in self.specs:
            invalidated.add("package_project")
        return invalidated

    def invalidate(self, *stage_ids: str) -> set[str]:
        """Invalidate selected stages and all explicit downstream dependencies."""

        invalidated: set[str] = set()
        for stage_id in stage_ids:
            if stage_id not in self.specs:
                raise ValueError(f"Unknown generation stage '{stage_id}'.")
            invalidated.update(self._retry_closure(stage_id))
        self.invalidated.update(invalidated)
        self._clear_records(invalidated)
        self.checkpoint()
        return invalidated

    def _clear_records(self, stage_ids: set[str]) -> None:
        for stage_id in stage_ids:
            record = self.records[stage_id]
            record.status = GenerationStageStatus.NOT_STARTED
            record.input_artifact_ids = []
            record.input_fingerprint = None
            record.output_fingerprint = None
            record.artifact_id = None
            record.artifact = None
            record.output = None
            record.error = None
            record.started_at = None
            record.completed_at = None

    def _dependency_fingerprint(self, stage_id: str) -> str:
        record = self.records[stage_id]
        payload = []
        for dependency in record.dependencies:
            dependency_record = self.records[dependency]
            payload.append({
                "stage_id": dependency,
                "artifact_id": dependency_record.artifact_id,
                "checksum": dependency_record.artifact.checksum if dependency_record.artifact else None,
                "output_fingerprint": dependency_record.output_fingerprint,
            })
        return _stage_output_checksum(payload)

    def _invalidate_fingerprint_mismatches(self) -> None:
        """Invalidate stale cached stages whose dependency artifacts changed."""

        stale: set[str] = set()
        for stage_id, record in self.records.items():
            if record.status != GenerationStageStatus.SUCCEEDED or not record.input_fingerprint:
                continue
            expected = self._dependency_fingerprint(stage_id)
            if expected != record.input_fingerprint:
                stale.update(self._retry_closure(stage_id))
        if stale:
            self.invalidated.update(stale)
            self._clear_records(stale)

    def checkpoint(self) -> None:
        if self.persist is not None:
            self.persist(self, None)

    def snapshot(self, *, include_outputs: bool = True) -> dict[str, Any]:
        records = {
            stage_id: record.model_dump(mode="json", exclude={"output"} if not include_outputs else None)
            for stage_id, record in self.records.items()
        }
        return {
            "generation_run_id": self.run_id,
            "workflow": self.workflow,
            "retry_stage": self.retry_stage,
            "status": self.overall_status,
            "records": records,
        }

    @property
    def overall_status(self) -> str:
        statuses = {record.status for record in self.records.values()}
        if statuses and statuses <= {GenerationStageStatus.SUCCEEDED}:
            return "succeeded"
        if GenerationStageStatus.FAILED in statuses:
            return "partial" if GenerationStageStatus.SUCCEEDED in statuses else "failed"
        if GenerationStageStatus.WAITING_FOR_APPROVAL in statuses:
            return "waiting_for_approval"
        if GenerationStageStatus.BLOCKED in statuses and GenerationStageStatus.SUCCEEDED in statuses:
            return "partial"
        return "running"

    def output(self, stage_id: str, schema: Any = None) -> Any:
        record = self.records[stage_id]
        if record.status != GenerationStageStatus.SUCCEEDED:
            return None
        return schema.model_validate(record.output) if schema is not None else record.output

    def wait_for_approval(
        self,
        stage_id: str,
        *,
        output: Any = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist a first-class resumable approval gate instead of blocking a worker."""

        if stage_id not in self.records:
            raise ValueError(f"Unknown generation stage '{stage_id}'.")
        record = self.records[stage_id]
        if not record.approval_gate:
            raise ValueError(f"Generation stage '{stage_id}' is not configured as an approval gate.")
        unsatisfied = [
            dependency
            for dependency in record.dependencies
            if self.records[dependency].status != GenerationStageStatus.SUCCEEDED
        ]
        if unsatisfied:
            raise ValueError(
                f"Generation approval stage '{stage_id}' has unsatisfied dependencies: {', '.join(unsatisfied)}."
            )
        record.status = GenerationStageStatus.WAITING_FOR_APPROVAL
        record.output = _stage_json_value(output) if output is not None else None
        record.input_artifact_ids = [
            str(self.records[dependency].artifact_id)
            for dependency in record.dependencies
            if self.records[dependency].artifact_id
        ]
        record.input_fingerprint = self._dependency_fingerprint(stage_id)
        record.error = None
        record.started_at = record.started_at or _utc_now()
        record.completed_at = None
        self._commit(record)
        emit_agent_pipeline_event(
            self.workflow,
            stage_id,
            "waiting_for_approval",
            details={
                "generation_stage": record.model_dump(mode="json"),
                **(details or {}),
            },
        )

    def approve(self, stage_id: str, *, output: Any = None) -> Any:
        """Complete a previously waiting approval stage without replaying dependencies."""

        if stage_id not in self.records:
            raise ValueError(f"Unknown generation stage '{stage_id}'.")
        record = self.records[stage_id]
        if not record.approval_gate:
            raise ValueError(f"Generation stage '{stage_id}' is not configured as an approval gate.")
        if record.status != GenerationStageStatus.WAITING_FOR_APPROVAL:
            raise ValueError(f"Generation stage '{stage_id}' is not waiting for approval.")
        if output is not None:
            record.output = _stage_json_value(output)
        record.output_fingerprint = _stage_output_checksum(record.output)
        record.artifact_id = f"{self.run_id}:{stage_id}:approval:{max(1, record.attempt)}"
        record.artifact = ProjectArtifact(
            artifact_id=record.artifact_id,
            kind=f"generation-stage:{stage_id}",
            uri=f"forma://generation-runs/{self.run_id}/stages/{stage_id}/approval",
            media_type="application/json",
            checksum=record.output_fingerprint,
            metadata={
                "generation_run_id": self.run_id,
                "stage_id": stage_id,
                "dependencies": list(record.dependencies),
                "input_artifact_ids": list(record.input_artifact_ids),
                "input_fingerprint": record.input_fingerprint,
                "fidelity": record.fidelity.value,
                "cost": record.cost.model_dump(mode="json"),
                "approval_gate": True,
            },
        )
        record.status = GenerationStageStatus.SUCCEEDED
        record.completed_at = _utc_now()
        self._archive_attempt(record)
        self._commit(record)
        emit_agent_pipeline_event(
            self.workflow,
            stage_id,
            "completed",
            details={"generation_stage": record.model_dump(mode="json")},
        )
        return record.output

    def run(self, stage_id: str, producer: Callable[[], Any], *, schema: Any = None) -> Any:
        ensure_agent_pipeline_active()
        if stage_id not in self.records:
            raise ValueError(f"Unknown generation stage '{stage_id}'.")
        record = self.records[stage_id]
        if record.status == GenerationStageStatus.SUCCEEDED:
            return self.output(stage_id, schema)
        if record.status == GenerationStageStatus.WAITING_FOR_APPROVAL:
            return None

        unsatisfied = [
            dependency
            for dependency in record.dependencies
            if self.records[dependency].status != GenerationStageStatus.SUCCEEDED
        ]
        if unsatisfied:
            record.status = GenerationStageStatus.BLOCKED
            record.error = {
                "code": "generation_stage_dependency_failed",
                "message": "One or more required generation stages did not succeed.",
                "dependency_stage_ids": unsatisfied,
            }
            record.completed_at = _utc_now()
            self._archive_attempt(record)
            self._commit(record)
            emit_agent_pipeline_event(
                self.workflow,
                stage_id,
                "blocked",
                details={"generation_stage": record.model_dump(mode="json")},
            )
            return None

        previous_attempt = record.attempt
        record.status = GenerationStageStatus.RUNNING
        record.attempt = previous_attempt + 1
        record.input_artifact_ids = [
            str(self.records[dependency].artifact_id)
            for dependency in record.dependencies
            if self.records[dependency].artifact_id
        ]
        record.input_fingerprint = self._dependency_fingerprint(stage_id)
        record.output_fingerprint = None
        record.error = None
        record.started_at = _utc_now()
        record.completed_at = None
        self._commit(record)
        emit_agent_pipeline_event(
            self.workflow,
            stage_id,
            "started",
            details={
                "attempt": record.attempt,
                "input_artifact_ids": record.input_artifact_ids,
                "input_fingerprint": record.input_fingerprint,
                "fidelity": record.fidelity.value,
                "cost": record.cost.model_dump(mode="json"),
            },
        )
        try:
            result = producer()
            record.output = _stage_json_value(result)
            record.output_fingerprint = _stage_output_checksum(record.output)
            record.artifact_id = f"{self.run_id}:{stage_id}:attempt:{record.attempt}"
            record.artifact = ProjectArtifact(
                artifact_id=record.artifact_id,
                kind=f"generation-stage:{stage_id}",
                uri=f"forma://generation-runs/{self.run_id}/stages/{stage_id}/attempts/{record.attempt}",
                media_type="application/json",
                checksum=record.output_fingerprint,
                metadata={
                    "generation_run_id": self.run_id,
                    "stage_id": stage_id,
                    "attempt": record.attempt,
                    "dependencies": list(record.dependencies),
                    "input_artifact_ids": list(record.input_artifact_ids),
                    "input_fingerprint": record.input_fingerprint,
                    "fidelity": record.fidelity.value,
                    "cost": record.cost.model_dump(mode="json"),
                    "approval_gate": record.approval_gate,
                },
            )
            record.status = GenerationStageStatus.SUCCEEDED
            record.completed_at = _utc_now()
            self._archive_attempt(record)
            self._commit(record)
        except PipelineCancelledError:
            raise
        except Exception as exc:
            record.output = None
            record.output_fingerprint = None
            record.artifact_id = None
            record.artifact = None
            record.status = GenerationStageStatus.FAILED
            record.error = {
                "code": "generation_stage_failed",
                "message": str(exc) or exc.__class__.__name__,
                "exception_type": exc.__class__.__name__,
            }
            record.completed_at = _utc_now()
            self._archive_attempt(record)
            self._commit(record)
            emit_agent_pipeline_event(
                self.workflow,
                stage_id,
                "failed",
                details={"generation_stage": record.model_dump(mode="json")},
            )
            return None

        emit_agent_pipeline_event(
            self.workflow,
            stage_id,
            "completed",
            details={"generation_stage": record.model_dump(mode="json")},
        )
        return schema.model_validate(record.output) if schema is not None else result

    def _commit(self, record: GenerationStageRecord) -> None:
        if self.persist is not None:
            self.persist(self, record)

    @staticmethod
    def _archive_attempt(record: GenerationStageRecord) -> None:
        entry = {
            "attempt": record.attempt,
            "status": record.status.value,
            "artifact_id": record.artifact_id,
            "artifact": record.artifact.model_dump(mode="json") if record.artifact is not None else None,
            "input_fingerprint": record.input_fingerprint,
            "output_fingerprint": record.output_fingerprint,
            "output": record.output,
            "error": record.error,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
        }
        if record.attempt_history and (
            record.attempt_history[-1].get("attempt") == record.attempt
            and record.attempt_history[-1].get("status") == record.status.value
        ):
            record.attempt_history[-1] = entry
        else:
            record.attempt_history.append(entry)


def _stage_json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_stage_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_stage_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stage_json_value(item) for key, item in value.items()}
    return value


def _stage_output_checksum(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class PipelineCancelledError(RuntimeError):
    """Raised when a caller cancels an active agent pipeline."""


_PIPELINE_EVENT_CALLBACK: contextvars.ContextVar[Optional[PipelineEventCallback]] = contextvars.ContextVar(
    "forma_pipeline_event_callback",
    default=None,
)
_PIPELINE_CANCELLATION_CHECK: contextvars.ContextVar[Optional[PipelineCancellationCheck]] = contextvars.ContextVar(
    "forma_pipeline_cancellation_check",
    default=None,
)


DEFAULT_AGENT_PIPELINE_STEPS = [
    AgentPipelineStep(
        id="safety_guardrail",
        agent="Safety Guardrail",
        label="Checking safe build scope",
        description="Screening the request for low-voltage maker hardware constraints.",
        duration_ms=3500,
    ),
    AgentPipelineStep(
        id="context_clarifier",
        agent="Context Clarifier Agent",
        label="Clarifying build context",
        description="Checking whether user-provided answers should be folded into the generation prompt.",
        duration_ms=2500,
    ),
    AgentPipelineStep(
        id="intent_parser",
        agent="Intent Parser Agent",
        label="Parsing the hardware idea",
        description="Converting the prompt into a project title, category, and build intent.",
    ),
    AgentPipelineStep(
        id="requirements",
        agent="Requirements Agent",
        label="Extracting requirements",
        description="Capturing functions, voltage, physical constraints, safety notes, and missing information.",
    ),
    AgentPipelineStep(
        id="system_architecture",
        agent="System Architecture Agent",
        label="Decomposing the complete system",
        description="Building a purpose-driven tree of electrical, mechanical, firmware, and nested subsystems.",
        duration_ms=5500,
    ),
    AgentPipelineStep(
        id="component_selection",
        agent="Component Selection Agent",
        label="Selecting compatible parts",
        description="Choosing catalog components and pin definitions that satisfy the requirements.",
        duration_ms=6500,
    ),
    AgentPipelineStep(
        id="wiring_netlist",
        agent="Wiring/Netlist Agent",
        label="Drafting nets and pin mappings",
        description="Connecting power, ground, buses, sensors, actuators, displays, and controller pins.",
        duration_ms=6500,
    ),
    AgentPipelineStep(
        id="validation_repair",
        agent="Validation + Auto-Correction Agent",
        label="Validating and repairing wiring",
        description="Checking for shorts, voltage mismatches, unpowered parts, and pin conflicts.",
        duration_ms=5500,
    ),
    AgentPipelineStep(
        id="bom",
        agent="BOM Agent",
        label="Calculating BOM and cost",
        description="Summing selected components and updating the project estimate.",
        duration_ms=3000,
    ),
    AgentPipelineStep(
        id="mechanical_fabrication",
        agent="Mechanical/Fabrication Agent",
        label="Designing enclosure and placement",
        description="Generating mounting, fabrication, CAD sourcing, and 3D render placement details.",
        duration_ms=6500,
    ),
    AgentPipelineStep(
        id="assembly",
        agent="Assembly Instruction Agent",
        label="Writing build steps",
        description="Producing sequential assembly instructions and safety flags.",
        duration_ms=5500,
    ),
    AgentPipelineStep(
        id="cad_generation",
        agent="OpenCAD Authoring Agent",
        label="Authoring native CAD",
        description="Converting the agent-authored mechanical design into a native OpenCAD artifact.",
        duration_ms=9000,
        optional=True,
    ),
    AgentPipelineStep(
        id="package_project",
        agent="Project Packager",
        label="Packaging project artifacts",
        description="Building the Hardware Intermediate Representation, diagrams, validation summary, and saved project record.",
        duration_ms=3500,
    ),
]


WEB_RESEARCH_AGENT_PIPELINE_STEPS = [
    AgentPipelineStep(
        id="safety_guardrail",
        agent="Safety Guardrail",
        label="Checking safe build scope",
        description="Screening the request for low-voltage maker hardware constraints.",
        duration_ms=3500,
    ),
    AgentPipelineStep(
        id="context_clarifier",
        agent="Context Clarifier Agent",
        label="Clarifying build context",
        description="Checking whether user-provided answers should be folded into sourced generation.",
        duration_ms=2500,
    ),
    AgentPipelineStep(
        id="external_research",
        agent="External Source Research Agent",
        label="Gathering source context",
        description="Searching for reference designs, components, datasheets, and build context.",
        duration_ms=7500,
    ),
    AgentPipelineStep(
        id="web_architect",
        agent="Web Research Hardware Architect Agent",
        label="Planning sourced architecture",
        description="Turning source context into requirements, architecture notes, and component roles.",
        duration_ms=6500,
    ),
    AgentPipelineStep(
        id="web_component_sourcing",
        agent="Web Component Sourcing Agent",
        label="Selecting sourced components",
        description="Choosing real components with sourcing notes and pin definitions.",
        duration_ms=7500,
    ),
    AgentPipelineStep(
        id="wiring_netlist",
        agent="Wiring/Netlist Agent",
        label="Drafting nets and pin mappings",
        description="Connecting sourced components into safe low-voltage electrical nets.",
        duration_ms=6500,
    ),
    AgentPipelineStep(
        id="validation_repair",
        agent="Validation + Auto-Correction Agent",
        label="Validating and repairing wiring",
        description="Checking and correcting electrical or logical issues.",
        duration_ms=5500,
    ),
    AgentPipelineStep(
        id="mechanical_fabrication",
        agent="Mechanical/Fabrication Agent",
        label="Designing enclosure and placement",
        description="Generating fabrication details, CAD sourcing, and 3D render placements.",
        duration_ms=6500,
    ),
    AgentPipelineStep(
        id="assembly",
        agent="Assembly Instruction Agent",
        label="Writing build steps",
        description="Producing build instructions grounded in the sourced parts and generated wiring.",
        duration_ms=5500,
    ),
    AgentPipelineStep(
        id="cad_generation",
        agent="OpenCAD Authoring Agent",
        label="Authoring native CAD",
        description="Converting the sourced mechanical design into a native OpenCAD artifact.",
        duration_ms=9000,
        optional=True,
    ),
    AgentPipelineStep(
        id="completeness_audit",
        agent="Hardware Output Completeness Auditor Agent",
        label="Auditing completeness",
        description="Checking for missing power, protection, sourcing, wiring, and assembly details.",
        duration_ms=5500,
    ),
    AgentPipelineStep(
        id="package_project",
        agent="Project Packager",
        label="Packaging project artifacts",
        description="Building the Hardware Intermediate Representation, diagrams, validation summary, and saved project record.",
        duration_ms=3500,
    ),
]


IMAGE_OUTPUT_PIPELINE_STEP = AgentPipelineStep(
    id="image_generation",
    agent="Product Image Agent",
    label="Generating product visuals",
    description="Creating optional concept images from the completed HardwareIntermediateRepresentation visual spec.",
    duration_ms=8000,
    optional=True,
)


def list_agent_pipeline_steps(
    workflow: Optional[str] = None,
    *,
    include_image: bool = False,
) -> List[dict[str, Any]]:
    normalized = normalize_generation_workflow_id(workflow, strict=False)
    steps = WEB_RESEARCH_AGENT_PIPELINE_STEPS if normalized == WEB_RESEARCH_WORKFLOW_ID else DEFAULT_AGENT_PIPELINE_STEPS
    payload = []
    for step in steps:
        record = step.as_dict()
        record["fidelity"] = stage_fidelity(step.id).value
        record["cost"] = stage_cost(step.id).model_dump(mode="json")
        payload.append(record)
    if include_image:
        record = IMAGE_OUTPUT_PIPELINE_STEP.as_dict()
        record["fidelity"] = stage_fidelity("system_visual").value
        record["cost"] = stage_cost("system_visual").model_dump(mode="json")
        payload.append(record)
    return payload


def pipeline_workflow_id(workflow: Optional[str] = None) -> str:
    normalized = normalize_generation_workflow_id(workflow, strict=False)
    return normalized if normalized in {DEFAULT_WORKFLOW_ID, WEB_RESEARCH_WORKFLOW_ID} else DEFAULT_WORKFLOW_ID


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def pipeline_step_metadata(workflow: Optional[str], step_id: str) -> Optional[dict[str, Any]]:
    for step in list_agent_pipeline_steps(workflow, include_image=True):
        if step.get("id") == step_id:
            return step
    return None


def emit_agent_pipeline_event(
    workflow: Optional[str],
    step_id: str,
    status: str,
    *,
    details: Optional[dict[str, Any]] = None,
) -> Optional[AgentPipelineEvent]:
    callback = _PIPELINE_EVENT_CALLBACK.get()
    if callback is None:
        return None

    normalized_workflow = pipeline_workflow_id(workflow)
    step = pipeline_step_metadata(normalized_workflow, step_id) or {
        "agent": step_id.replace("_", " ").title(),
        "label": step_id.replace("_", " ").title(),
        "description": "",
    }
    event = AgentPipelineEvent(
        workflow=normalized_workflow,
        step_id=step_id,
        status=status,
        agent=str(step.get("agent") or step_id),
        label=str(step.get("label") or step_id),
        description=str(step.get("description") or ""),
        observed_at=_utc_now(),
        details={
            "fidelity": step.get("fidelity"),
            "cost": step.get("cost"),
            **(details or {}),
        },
    )
    callback(event)
    return event


@contextlib.contextmanager
def observe_agent_pipeline(
    callback: PipelineEventCallback,
    cancellation_check: Optional[PipelineCancellationCheck] = None,
) -> Iterator[None]:
    callback_token = _PIPELINE_EVENT_CALLBACK.set(callback)
    cancellation_token = _PIPELINE_CANCELLATION_CHECK.set(cancellation_check)
    try:
        yield
    finally:
        _PIPELINE_CANCELLATION_CHECK.reset(cancellation_token)
        _PIPELINE_EVENT_CALLBACK.reset(callback_token)


def ensure_agent_pipeline_active() -> None:
    cancellation_check = _PIPELINE_CANCELLATION_CHECK.get()
    if cancellation_check is not None and cancellation_check():
        raise PipelineCancelledError("Agent pipeline was cancelled.")


@contextlib.contextmanager
def agent_pipeline_step(
    workflow: Optional[str],
    step_id: str,
    *,
    details: Optional[dict[str, Any]] = None,
) -> Iterator[None]:
    emit_agent_pipeline_event(workflow, step_id, "started", details=details)
    try:
        yield
    except Exception as exc:
        emit_agent_pipeline_event(
            workflow,
            step_id,
            "failed",
            details={**(details or {}), "error_type": exc.__class__.__name__, "error": str(exc)[:500]},
        )
        raise
    else:
        emit_agent_pipeline_event(workflow, step_id, "completed", details=details)
