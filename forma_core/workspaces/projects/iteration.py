from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from forma_core.llm import (
    LLMProviderConfigError,
    LLMProviderOutputError,
    LLMProviderValidation,
    LLMRuntimeConfig,
    StructuredLLMProvider,
    build_llm_provider,
    enforce_production_llm_preflight,
    resolve_llm_runtime_config,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.objects import (
    DEFAULT_PROJECT_NAMESPACES,
    attach_project_object_metadata,
    normalize_project_namespace,
    namespace_payload,
)
from forma_core.validation import build_validation_summary, validate_circuit
from forma_core.user_integrations import ResolvedIntegrationSettings


logger = logging.getLogger(__name__)

PLACEHOLDER_TEXT_VALUES = {"", "unknown", "n/a", "na", "none", "null", "new", "new__rewrite_1"}
DEFAULT_CONTEXT_MAX_STRING_CHARS = 4000


@dataclass(frozen=True)
class ProjectIterationMetadata:
    """Debuggable summary of one project iteration operation."""

    mode: str
    revision: int
    previous_revision: int
    instruction: str
    provider: str
    model: Optional[str]
    live_generation_enabled: bool
    target_namespace: Optional[str] = None
    validation_error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "revision": self.revision,
            "previous_revision": self.previous_revision,
            "instruction": self.instruction,
            "provider": self.provider,
            "model": self.model,
            "live_generation_enabled": self.live_generation_enabled,
            "target_namespace": self.target_namespace,
            "validation_error": self.validation_error,
        }


@dataclass(frozen=True)
class ProjectSelfCorrectionPlan:
    target_namespace: str
    instruction: str
    critical_issue_count: int
    warning_issue_count: int
    output_issue_count: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target_namespace": self.target_namespace,
            "instruction": self.instruction,
            "critical_issue_count": self.critical_issue_count,
            "warning_issue_count": self.warning_issue_count,
            "output_issue_count": self.output_issue_count,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def coerce_hardware_ir(value: HardwareIntermediateRepresentation | Dict[str, Any]) -> HardwareIntermediateRepresentation:
    if isinstance(value, HardwareIntermediateRepresentation):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        return HardwareIntermediateRepresentation.model_validate(value)
    raise TypeError("current project must be a HardwareIntermediateRepresentation or hardware intermediate representation dictionary.")


def normalize_iteration_instruction(value: str) -> str:
    instruction = (value or "").strip()
    if not instruction:
        raise ValueError("Iteration instruction is required.")
    return instruction


def _metadata_revision(ir: HardwareIntermediateRepresentation) -> int:
    metadata = ir.assembly_metadata or {}
    raw_value = metadata.get("revision")
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        history_count = len(ir.project_version_history or [])
        return max(1, history_count)


def next_revision_number(ir: HardwareIntermediateRepresentation) -> int:
    return _metadata_revision(ir) + 1


def _project_id_from_metadata(ir: HardwareIntermediateRepresentation, override: Optional[str] = None) -> str:
    value = override or (ir.assembly_metadata or {}).get("project_id")
    if value:
        try:
            return str(uuid.UUID(str(value).strip()))
        except (TypeError, ValueError, AttributeError):
            pass
    return str(uuid.uuid4())


def _is_placeholder_text(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in PLACEHOLDER_TEXT_VALUES


def _redact_context_value(value: Any, *, key: str = "", max_string_chars: int = DEFAULT_CONTEXT_MAX_STRING_CHARS) -> Any:
    lowered_key = key.lower()
    if isinstance(value, dict):
        return {
            item_key: _redact_context_value(item_value, key=item_key, max_string_chars=max_string_chars)
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_context_value(item, max_string_chars=max_string_chars) for item in value]
    if isinstance(value, str):
        if ("image" in lowered_key or "data" in lowered_key) and value.startswith("data:"):
            return f"<redacted data url: {len(value)} chars>"
        if len(value) > max_string_chars:
            return value[:max_string_chars] + f"...<truncated {len(value) - max_string_chars} chars>"
    return value


def compact_hardware_ir_for_iteration(ir: HardwareIntermediateRepresentation, *, max_string_chars: int = DEFAULT_CONTEXT_MAX_STRING_CHARS) -> Dict[str, Any]:
    """Return an LLM-safe project context with large data URLs and huge fields compacted."""
    return _redact_context_value(ir.model_dump(mode="json", exclude_none=True), max_string_chars=max_string_chars)


def build_iteration_prompt(
    current_ir: HardwareIntermediateRepresentation,
    instruction: str,
    *,
    original_prompt: Optional[str] = None,
    project_id: Optional[str] = None,
    target_namespace: Optional[str] = None,
) -> str:
    compact_ir = compact_hardware_ir_for_iteration(current_ir)
    previous_revision = _metadata_revision(current_ir)
    next_revision = previous_revision + 1
    normalized_namespace = normalize_project_namespace(target_namespace)
    namespace_block = ""
    if normalized_namespace:
        boundary_guidance = ""
        if normalized_namespace == "product.mech":
            boundary_guidance = (
                "A product.mech change may revise physical_form, enclosure_type, dimensions, placement, materials, "
                "and fabrication details. Keep the existing BOM/components and all electrical connectivity fixed. "
                "Component or wiring changes must use the corresponding electrical or BOM namespace.\n"
            )
        namespace_block = (
            f"\nTarget namespace: {normalized_namespace}\n"
            "Focus the requested change on this namespace. Preserve other namespaces unless they must change to keep the project coherent.\n"
            f"{boundary_guidance}"
            "Current target namespace payload:\n"
            f"{json.dumps(namespace_payload(current_ir, normalized_namespace), indent=2, sort_keys=True)}\n"
        )
    return (
        "You are Forma's project iteration engine. Revise an existing Hardware Intermediate Representation project.\n"
        "Return one complete Hardware Intermediate Representation JSON document, not a patch and not markdown.\n"
        "Preserve every part of the project that the instruction does not explicitly change.\n"
        "Keep the existing project_id, reference designators, and stable net IDs unless a requested change requires updates.\n"
        "If you add, remove, or replace components, update components, nets, buses, pin_mappings, power_rails, "
        "requirements, assembly steps, mechanical placement, cost, validation-related fields, and fabrication notes together.\n"
        "Never claim unsupported functionality without adding the required component and wiring changes.\n"
        f"Set assembly_metadata.revision to {next_revision} and append a project_version_history entry for this iteration.\n\n"
        f"Project id: {project_id or (current_ir.assembly_metadata or {}).get('project_id') or 'unknown'}\n"
        f"Original prompt: {original_prompt or 'unknown'}\n"
        f"Iteration instruction: {instruction}\n\n"
        f"{namespace_block}"
        "Current HardwareIntermediateRepresentation JSON:\n"
        f"{json.dumps(compact_ir, indent=2, sort_keys=True)}"
    )


def _append_history_entry(
    ir: HardwareIntermediateRepresentation,
    *,
    instruction: str,
    revision: int,
    mode: str,
    provider: str,
    model: Optional[str],
    previous_revision: int,
) -> None:
    version = f"0.{revision}"
    history = [
        dict(item)
        for item in (ir.project_version_history or [])
        if isinstance(item, dict)
        and item.get("revision") != revision
        and item.get("version") != version
    ]
    history.append(
        {
            "version": version,
            "revision": revision,
            "previous_revision": previous_revision,
            "description": instruction,
            "change_type": "iteration",
            "mode": mode,
            "provider": provider,
            "model": model,
            "created_at": utc_now(),
        }
    )
    ir.project_version_history = history


def _metadata_value_missing(value: Any) -> bool:
    return value in (None, "", [], {})


def _should_preserve_output_metadata_key(key: str) -> bool:
    if key in {
        "external_research",
        "firecrawl_research",
        "operation_statuses",
        "operation_summary",
        "source_usage",
        "tavily_research",
    }:
        return True
    return (
        key.startswith("image_output")
        or key.startswith("product_image")
        or key.startswith("product_case_image")
        or key.startswith("product_inside_image")
        or key.startswith("product_visual")
        or key.startswith("reference_image")
    )


def _preserve_output_metadata(metadata: Dict[str, Any], base_metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Carry forward generated artifacts/source metadata when an iteration omits bulky output fields."""
    preserved = dict(metadata)
    for key, value in base_metadata.items():
        if not _should_preserve_output_metadata_key(str(key)):
            continue
        if key not in preserved or _metadata_value_missing(preserved.get(key)):
            preserved[key] = value
    return preserved


def _namespace_versions_for_iteration(
    *,
    base_ir: HardwareIntermediateRepresentation,
    current_metadata: Dict[str, Any],
    revision: int,
    previous_revision: int,
    target_namespace: Optional[str],
) -> Dict[str, int]:
    base_object = (base_ir.assembly_metadata or {}).get("project_object")
    base_versions = base_object.get("namespace_versions") if isinstance(base_object, dict) else None
    current_object = current_metadata.get("project_object")
    current_versions = current_object.get("namespace_versions") if isinstance(current_object, dict) else None
    namespace_names = list(DEFAULT_PROJECT_NAMESPACES.names)
    for source in (base_versions, current_versions):
        if isinstance(source, dict):
            for namespace in source:
                normalized = normalize_project_namespace(str(namespace))
                if normalized and normalized not in namespace_names:
                    namespace_names.append(normalized)

    if target_namespace:
        versions = {namespace: previous_revision for namespace in namespace_names}
        if isinstance(base_versions, dict):
            for namespace, raw_value in base_versions.items():
                normalized = normalize_project_namespace(str(namespace))
                if not normalized:
                    continue
                try:
                    versions[normalized] = max(1, int(raw_value))
                except (TypeError, ValueError):
                    versions[normalized] = previous_revision
        versions[target_namespace] = revision
        return versions

    return {namespace: revision for namespace in namespace_names}


def _normalize_iteration_metadata(
    ir: HardwareIntermediateRepresentation,
    *,
    base_ir: HardwareIntermediateRepresentation,
    instruction: str,
    mode: str,
    provider_validation: LLMProviderValidation,
    project_id: Optional[str],
    target_namespace: Optional[str],
) -> ProjectIterationMetadata:
    previous_revision = _metadata_revision(base_ir)
    revision = previous_revision + 1
    normalized_project_id = _project_id_from_metadata(base_ir, project_id)
    actual_model = provider_validation.actual_model or provider_validation.requested_model
    normalized_namespace = normalize_project_namespace(target_namespace)
    metadata = dict(ir.assembly_metadata or {})
    base_metadata = dict(base_ir.assembly_metadata or {})
    metadata = _preserve_output_metadata(metadata, base_metadata)
    if (
        _metadata_value_missing(metadata.get("chat_id"))
        and not _metadata_value_missing(base_metadata.get("chat_id"))
    ):
        metadata["chat_id"] = base_metadata["chat_id"]
    metadata.update(
        {
            "project_id": normalized_project_id,
            "revision": revision,
            "previous_revision": previous_revision,
            "iterated_at": utc_now(),
            "iteration_instruction": instruction,
            "iteration_target_namespace": normalized_namespace,
            "iteration_mode": mode,
            "iteration_provider": provider_validation.provider,
            "iteration_model": actual_model,
            "iteration_live_generation_enabled": provider_validation.live_generation_enabled,
            "iteration_validation_error": provider_validation.validation_error,
            "last_iteration": {
                "instruction": instruction,
                "mode": mode,
                "target_namespace": normalized_namespace,
                "previous_revision": previous_revision,
                "revision": revision,
                "provider": provider_validation.provider,
                "model": actual_model,
                "created_at": utc_now(),
            },
        }
    )
    object_metadata = dict(metadata.get("project_object") or {})
    object_metadata["namespace_versions"] = _namespace_versions_for_iteration(
        base_ir=base_ir,
        current_metadata=metadata,
        revision=revision,
        previous_revision=previous_revision,
        target_namespace=normalized_namespace,
    )
    metadata["project_object"] = object_metadata
    ir.assembly_metadata = metadata
    object_ir = attach_project_object_metadata(ir, target_namespace=normalized_namespace)
    ir.assembly_metadata = object_ir.assembly_metadata
    _append_history_entry(
        ir,
        instruction=instruction,
        revision=revision,
        mode=mode,
        provider=provider_validation.provider,
        model=actual_model,
        previous_revision=previous_revision,
    )
    return ProjectIterationMetadata(
        mode=mode,
        revision=revision,
        previous_revision=previous_revision,
        instruction=instruction,
        provider=provider_validation.provider,
        model=actual_model,
        live_generation_enabled=provider_validation.live_generation_enabled,
        target_namespace=normalized_namespace,
        validation_error=provider_validation.validation_error,
    )


def finalize_project_iteration(
    revised_ir: HardwareIntermediateRepresentation | Dict[str, Any],
    *,
    base_ir: HardwareIntermediateRepresentation | Dict[str, Any],
    instruction: str,
    provider_validation: LLMProviderValidation,
    project_id: Optional[str] = None,
    target_namespace: Optional[str] = None,
    mode: str = "llm",
) -> HardwareIntermediateRepresentation:
    base = coerce_hardware_ir(base_ir)
    revised = coerce_hardware_ir(revised_ir)
    instruction = normalize_iteration_instruction(instruction)
    normalized_namespace = normalize_project_namespace(target_namespace)

    if normalized_namespace == "product.mech":
        # A mechanical chat iteration is the shape-redesign path. Enforce the
        # namespace boundary even if a model returns opportunistic BOM edits.
        for field_name in (
            "components",
            "nets",
            "buses",
            "pin_mappings",
            "power_rails",
            "estimated_current_draw_ma",
        ):
            setattr(revised, field_name, getattr(base, field_name))

    if revised.overview and (_is_placeholder_text(revised.overview.title) or _is_placeholder_text(revised.overview.description)):
        raise LLMProviderOutputError(
            "Project iteration output was unusable: the selected model returned placeholder overview fields "
            f"(title={revised.overview.title!r}, description={revised.overview.description!r})."
        )

    issues = validate_circuit(revised.components, revised.nets, revised.requirements)
    revised.validation = build_validation_summary(issues)
    revised.is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in issues)
    _normalize_iteration_metadata(
        revised,
        base_ir=base,
        instruction=instruction,
        mode=mode,
        provider_validation=provider_validation,
        project_id=project_id,
        target_namespace=target_namespace,
    )
    return revised


def build_metadata_only_iteration(
    current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
    instruction: str,
    *,
    provider_validation: LLMProviderValidation,
    project_id: Optional[str] = None,
    target_namespace: Optional[str] = None,
) -> HardwareIntermediateRepresentation:
    """Record an iteration request without pretending hardware content changed."""
    base = coerce_hardware_ir(current_ir)
    revised = base.model_copy(deep=True)
    metadata = dict(revised.assembly_metadata or {})
    requested = metadata.get("pending_iteration_instructions")
    pending = [item for item in requested if isinstance(item, dict)] if isinstance(requested, list) else []
    pending.append(
        {
            "instruction": normalize_iteration_instruction(instruction),
            "target_namespace": normalize_project_namespace(target_namespace),
            "created_at": utc_now(),
        }
    )
    metadata["pending_iteration_instructions"] = pending
    revised.assembly_metadata = metadata
    return finalize_project_iteration(
        revised,
        base_ir=base,
        instruction=instruction,
        provider_validation=provider_validation,
        project_id=project_id,
        target_namespace=target_namespace,
        mode="metadata-only",
    )


def _metadata_output_findings(ir: HardwareIntermediateRepresentation) -> list[str]:
    metadata = dict(ir.assembly_metadata or {})
    findings: list[str] = []

    external_research = metadata.get("external_research")
    if isinstance(external_research, dict):
        provider = str(external_research.get("provider") or "external source")
        error = external_research.get("error")
        source_count = external_research.get("source_count")
        configured = bool(external_research.get("configured"))
        if error:
            findings.append(f"External source research via {provider} reported an error: {error}")
        if configured and provider in {"firecrawl", "tavily"} and int(source_count or 0) == 0:
            findings.append(f"External source research via {provider} produced zero usable source records.")

    image_status = metadata.get("image_output_status")
    if metadata.get("image_output_requested") and image_status != "succeeded":
        findings.append(
            "Image generation was requested but did not succeed"
            + (f": {metadata.get('image_output_error')}" if metadata.get("image_output_error") else ".")
        )
    if image_status == "succeeded" and metadata.get("image_output_error"):
        findings.append(f"Image metadata still contains a stale error after success: {metadata.get('image_output_error')}")
    if image_status == "succeeded" and not metadata.get("product_image_url") and not metadata.get("product_image_data"):
        findings.append("Image metadata says generation succeeded, but no primary product image URL/data is present.")
    if image_status == "succeeded" and int(metadata.get("product_visual_sequence_count") or 0) <= 0:
        findings.append("Image metadata says generation succeeded, but product_visual_sequence_count is empty.")

    for record in metadata.get("product_visual_sequence") or []:
        if not isinstance(record, dict):
            continue
        if record.get("storage_error"):
            view_id = record.get("view_id") or "unknown"
            findings.append(f"Generated image view {view_id!r} has a storage error: {record.get('storage_error')}")

    for operation in metadata.get("operation_statuses") or []:
        if not isinstance(operation, dict):
            continue
        status = str(operation.get("status") or "").lower()
        if status in {"failed", "pending"}:
            label = operation.get("label") or operation.get("id") or "operation"
            error = operation.get("error") or operation.get("reason") or "no detail"
            findings.append(f"Operation {label!r} is {status}: {error}")

    return findings


def _stored_validation_issues(ir: HardwareIntermediateRepresentation) -> list[Any]:
    validation = ir.validation
    return [
        *list(validation.critical or []),
        *list(validation.warning or []),
    ]


def _dedupe_validation_issues(issues: list[Any]) -> list[Any]:
    deduped: list[Any] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (
            str(getattr(issue, "severity", "") or "").upper(),
            str(getattr(issue, "category", "") or ""),
            str(getattr(issue, "description", "") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


class ProjectIterator:
    """Provider-agnostic project revision engine for HardwareIntermediateRepresentation documents."""

    def __init__(
        self,
        *,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        runtime_config: Optional[LLMRuntimeConfig] = None,
        settings: Optional[ResolvedIntegrationSettings] = None,
        llm_provider: Optional[StructuredLLMProvider] = None,
        use_simulation: bool = False,
        require_live_generation: bool = False,
    ) -> None:
        self.runtime_config = runtime_config or resolve_llm_runtime_config(
            provider_name=provider_name,
            model_name=model_name,
            settings=settings,
        )
        self.llm_provider = llm_provider or build_llm_provider(runtime_config=self.runtime_config, settings=settings)
        self.use_simulation = use_simulation or not self.llm_provider.is_configured
        self.require_live_generation = require_live_generation

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        validation = self.llm_provider.validate_configured_model(raise_on_strict=raise_on_strict)
        return enforce_production_llm_preflight(validation)

    def get_debug_config(self) -> Dict[str, Any]:
        return {
            **self.validate_configured_model(raise_on_strict=False).as_debug_dict(),
            "runtime": self.runtime_config.as_debug_dict(),
            "operation": "project_iteration",
            "use_simulation": self.use_simulation,
        }

    def iterate_project(
        self,
        current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
        instruction: str,
        *,
        original_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
        target_namespace: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
    ) -> HardwareIntermediateRepresentation:
        base = coerce_hardware_ir(current_ir)
        instruction = normalize_iteration_instruction(instruction)
        normalized_namespace = normalize_project_namespace(target_namespace)

        try:
            validation = self.validate_configured_model()
        except LLMProviderConfigError:
            raise

        if self.use_simulation or not validation.live_generation_enabled:
            if self.require_live_generation:
                raise LLMProviderConfigError(
                    "Project iteration requires a live LLM provider; CLI fallback output is disabled. "
                    "Configure the requested provider or pass --simulation explicitly."
                )
            logger.info("Project iteration using metadata-only mode: provider=%s model=%s", validation.provider, validation.actual_model)
            return build_metadata_only_iteration(
                base,
                instruction,
                provider_validation=validation,
                project_id=project_id,
                target_namespace=normalized_namespace,
            )

        prompt = build_iteration_prompt(
            base,
            instruction,
            original_prompt=original_prompt,
            project_id=project_id,
            target_namespace=normalized_namespace,
        )
        try:
            revised = self.llm_provider.generate_structured(prompt, HardwareIntermediateRepresentation, image_bytes, image_mime_type)
        except Exception as exc:
            raise LLMProviderOutputError(
                f"Project iteration failed for provider={validation.provider} model={validation.actual_model or validation.requested_model}: {exc}"
            ) from exc

        return finalize_project_iteration(
            revised,
            base_ir=base,
            instruction=instruction,
            provider_validation=validation,
            project_id=project_id,
            target_namespace=normalized_namespace,
            mode="llm",
        )


def iterate_project(
    current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
    instruction: str,
    *,
    original_prompt: Optional[str] = None,
    project_id: Optional[str] = None,
    target_namespace: Optional[str] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    runtime_config: Optional[LLMRuntimeConfig] = None,
    settings: Optional[ResolvedIntegrationSettings] = None,
    llm_provider: Optional[StructuredLLMProvider] = None,
    use_simulation: bool = False,
    require_live_generation: bool = False,
) -> HardwareIntermediateRepresentation:
    iterator = ProjectIterator(
        provider_name=provider_name,
        model_name=model_name,
        runtime_config=runtime_config,
        settings=settings,
        llm_provider=llm_provider,
        use_simulation=use_simulation,
        require_live_generation=require_live_generation,
    )
    return iterator.iterate_project(
        current_ir,
        instruction,
        original_prompt=original_prompt,
        project_id=project_id,
        target_namespace=target_namespace,
    )


__all__ = [
    "ProjectIterationMetadata",
    "ProjectIterator",
    "ProjectSelfCorrectionPlan",
    "build_iteration_prompt",
    "build_metadata_only_iteration",
    "compact_hardware_ir_for_iteration",
    "coerce_hardware_ir",
    "finalize_project_iteration",
    "iterate_project",
    "next_revision_number",
    "normalize_iteration_instruction",
]
