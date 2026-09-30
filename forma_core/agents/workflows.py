from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from forma_core.agents.orchestrator import HardwarePipelineOrchestrator
from forma_core.agents.web_research_workflow import WebResearchHardwarePipeline
from forma_core.jobs.source_usage import (
    DEFAULT_WORKFLOW_ID,
    WEB_RESEARCH_WORKFLOW_ID,
    normalize_generation_workflow_id,
    source_usage_for_workflow,
)
from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.user_integrations import ResolvedIntegrationSettings


@dataclass(frozen=True)
class WorkflowDescriptor:
    id: str
    label: str
    description: str
    uses_catalog: bool = False
    uses_web_research: bool = False
    uses_firecrawl_mcp: bool = False
    uses_external_sources: bool = False


WORKFLOW_DESCRIPTORS = [
    WorkflowDescriptor(
        id=DEFAULT_WORKFLOW_ID,
        label="Catalog",
        description="Original sequential pipeline constrained to the active component catalog.",
        uses_catalog=True,
    ),
    WorkflowDescriptor(
        id=WEB_RESEARCH_WORKFLOW_ID,
        label="Web Research",
        description="Firecrawl-backed source research pipeline that gathers component context before validation.",
        uses_web_research=True,
        uses_firecrawl_mcp=True,
        uses_external_sources=True,
    ),
]


def normalize_workflow_id(value: Optional[str]) -> str:
    return normalize_generation_workflow_id(value, strict=True)


def list_workflows() -> List[Dict[str, Any]]:
    return [descriptor.__dict__ for descriptor in WORKFLOW_DESCRIPTORS]


def get_workflow_debug_config(
    workflow_id: Optional[str] = None,
    *,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    external_source_provider: Optional[str] = None,
    settings: Optional[ResolvedIntegrationSettings] = None,
) -> Dict[str, Any]:
    normalized = normalize_workflow_id(workflow_id)
    if normalized == WEB_RESEARCH_WORKFLOW_ID:
        return WebResearchHardwarePipeline(
            provider_name=provider_name,
            model_name=model_name,
            external_source_provider=external_source_provider,
            settings=settings,
        ).get_debug_config()
    return {
        **HardwarePipelineOrchestrator(provider_name=provider_name, model_name=model_name, settings=settings).get_debug_config(),
        "workflow": DEFAULT_WORKFLOW_ID,
    }


def generate_project_with_workflow(
    workflow_id: Optional[str],
    prompt: str,
    *,
    image_bytes: Optional[bytes] = None,
    image_mime_type: Optional[str] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    external_source_provider: Optional[str] = None,
    settings: Optional[ResolvedIntegrationSettings] = None,
    generation_metadata: Optional[Dict[str, Any]] = None,
    persist_project: bool = True,
) -> HardwareIntermediateRepresentation:
    normalized = normalize_workflow_id(workflow_id)
    source_usage = source_usage_for_workflow(normalized, external_provider=external_source_provider)
    if normalized == WEB_RESEARCH_WORKFLOW_ID:
        ir = WebResearchHardwarePipeline(
            provider_name=provider_name,
            model_name=model_name,
            external_source_provider=external_source_provider,
            settings=settings,
            persist_project=persist_project,
        ).generate_project(
            prompt,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            generation_metadata=generation_metadata,
        )
    else:
        ir = HardwarePipelineOrchestrator(
            provider_name=provider_name,
            model_name=model_name,
            persist_project=persist_project,
            settings=settings,
        ).generate_project(
            prompt,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            generation_metadata=generation_metadata,
        )
    public_generation_metadata = {
        key: value
        for key, value in (generation_metadata or {}).items()
        if key not in {"owner_user_id", "project_prompt"} and value is not None and value != ""
    }
    ir.assembly_metadata = {
        **(ir.assembly_metadata or {}),
        **public_generation_metadata,
        "workflow": normalized,
        "source_usage": (ir.assembly_metadata or {}).get("source_usage") or source_usage,
    }
    ir_metadata = ir.assembly_metadata or {}
    cad_required = (generation_metadata or {}).get("cad_required")
    if cad_required is None:
        cad_required = ir_metadata.get("cad_required") is True
    if cad_required is True and "cad_generation" not in ir_metadata:
        ensure_native_cad_model(
            ir,
            project_id=ir_metadata.get("project_id"),
            required=True,
            authoring_agent=ir_metadata.get("authoring_agent"),
            workflow=normalized,
        )
    return ir
