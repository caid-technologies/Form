from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_serializer

from forma_core.external_sources import ExternalSourceLibrary, build_external_source_provider
from forma_core.agents.orchestrator import (
    HardwarePipelineOrchestrator,
    build_mechanical_render_data,
    canonical_project_uuid,
    estimate_current_draw,
    extract_buses,
    extract_power_rails,
)
from forma_core.agents.system_architecture import (
    compact_component_context,
    compact_net_context,
    system_context,
)
from forma_core.database import persist_chat_project_revision, persist_legacy_project_projection, update_project_deletion_state
from forma_core.jobs.source_usage import source_usage_for_workflow
from forma_core.llm import (
    LLMProviderConfigError,
    LLMProviderValidation,
    LLMRuntimeConfig,
    build_llm_provider,
    enforce_production_llm_preflight,
    resolve_llm_runtime_config,
)
from forma_core.workspaces.projects.models import (
    AssemblyStep,
    ComponentInstance,
    ConnectionNet,
    FunctionalRequirements,
    HardwareIntermediateRepresentation,
    MechanicalNotes,
    PinMappingEntry,
    ProjectOverview,
    SystemArchitecture,
    ValidationIssue,
    component_detail_payload,
    component_instance_count,
    expand_component_instances,
)
from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model
from forma_core.observability import serialize_for_langfuse, start_observation, update_observation
from forma_core.agents.pipeline import (
    GenerationStageRun,
    GenerationStageSpec,
    PipelineCancelledError,
    agent_pipeline_step,
    emit_agent_pipeline_event,
    ensure_agent_pipeline_active,
)
from forma_core.runtime import (
    AlphaGenerationUnavailableError,
    deployment_mode_enabled,
    generation_unavailable_message,
)
from forma_core.user_integrations import ResolvedIntegrationSettings
from forma_core.validation import (
    build_validation_summary,
    check_safety_violations,
    validate_circuit,
)
from forma_core.wiring import (
    WiringIntent,
    build_endpoint_catalog,
    compile_wiring_intent,
    derive_pin_mappings,
    endpoint_catalog_prompt,
    wiring_failure_category,
)


logger = logging.getLogger(__name__)


class WebProjectPlan(BaseModel):
    overview: ProjectOverview
    requirements: FunctionalRequirements
    system_architecture: SystemArchitecture
    architecture_notes: List[str] = Field(default_factory=list)
    recommended_component_roles: List[str] = Field(default_factory=list)
    research_keywords: List[str] = Field(default_factory=list)


class WebComponentSelection(BaseModel):
    components: List[ComponentInstance]
    sourcing_notes: List[str] = Field(default_factory=list)
    rejected_options: List[str] = Field(default_factory=list)

    @field_serializer("components")
    def serialize_components(self, components: List[ComponentInstance]) -> list[dict[str, object]]:
        # Stage snapshots have no shared part_definitions to hydrate from.
        return [component_detail_payload(component) for component in components]


class WiringWrapper(BaseModel):
    nets: List[ConnectionNet]
    pin_mappings: List[PinMappingEntry]
    intent_issues: List[ValidationIssue] = Field(default_factory=list)


class AssemblyWrapper(BaseModel):
    steps: List[AssemblyStep]


class CompletenessAudit(BaseModel):
    completeness_score: float = Field(0.0, ge=0.0, le=1.0)
    missing_items: List[str] = Field(default_factory=list)
    possible_risks: List[str] = Field(default_factory=list)
    recommended_next_checks: List[str] = Field(default_factory=list)
    summary: str = ""


class ValidationStageOutput(BaseModel):
    nets: List[ConnectionNet]
    pin_mappings: List[PinMappingEntry]
    issues: List[ValidationIssue]
    is_valid: bool


WEB_GENERATION_STAGE_SPECS = [
    GenerationStageSpec(stage_id="external_research"),
    GenerationStageSpec(stage_id="web_architect", dependencies=["external_research"]),
    GenerationStageSpec(stage_id="web_component_sourcing", dependencies=["web_architect"]),
    GenerationStageSpec(stage_id="wiring_netlist", dependencies=["web_component_sourcing"]),
    GenerationStageSpec(stage_id="validation_repair", dependencies=["wiring_netlist"]),
    GenerationStageSpec(stage_id="mechanical_fabrication", dependencies=["web_component_sourcing"]),
    GenerationStageSpec(stage_id="assembly", dependencies=["wiring_netlist", "mechanical_fabrication"]),
    GenerationStageSpec(
        stage_id="completeness_audit",
        dependencies=["validation_repair", "mechanical_fabrication", "assembly"],
    ),
    GenerationStageSpec(
        stage_id="cad_generation",
        dependencies=["mechanical_fabrication", "assembly"],
    ),
    GenerationStageSpec(stage_id="package_project"),
]


class WebResearchHardwarePipeline:
    """Internet-researched hardware workflow that keeps the same HardwareIntermediateRepresentation output contract."""

    workflow_id = "web_research"

    def __init__(
        self,
        provider_name: Optional[str] = None,
        model_name: Optional[str] = None,
        runtime_config: Optional[LLMRuntimeConfig] = None,
        settings: Optional[ResolvedIntegrationSettings] = None,
        external_source_provider: Optional[str] = None,
        persist_project: bool = True,
    ):
        self.settings = settings
        self.runtime_config = runtime_config or resolve_llm_runtime_config(
            provider_name=provider_name,
            model_name=model_name,
            settings=settings,
        )
        self.llm_provider = build_llm_provider(runtime_config=self.runtime_config, settings=settings)
        self.use_simulation = not self.llm_provider.is_configured
        self.model_name = self.llm_provider.model_name
        self.external_source_provider = external_source_provider
        self.persist_project = persist_project
        self.research_client = build_external_source_provider(provider=external_source_provider, settings=settings)
        self._active_generation_metadata: Dict[str, Any] = {}

    def get_debug_config(self) -> Dict[str, Any]:
        validation = self.validate_configured_model(raise_on_strict=False)
        self.model_name = validation.actual_model or self.llm_provider.model_name
        return {
            **validation.as_debug_dict(),
            "runtime": self.runtime_config.as_debug_dict(),
            "workflow": self.workflow_id,
            "external_sources": self.research_client.get_debug_config(),
        }

    def validate_configured_model(self, *, raise_on_strict: bool = True) -> LLMProviderValidation:
        validation = self.llm_provider.validate_configured_model(raise_on_strict=raise_on_strict)
        return enforce_production_llm_preflight(validation)

    def _call_llm_structured(
        self,
        prompt: str,
        schema_class: Any,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
        pipeline_step_id: Optional[str] = None,
    ) -> Any:
        if self.use_simulation:
            raise RuntimeError("Simulation mode is active; web research workflow needs a live structured LLM provider.")

        schema_name = getattr(schema_class, "__name__", "StructuredResponse")
        metadata = {
            "workflow": self.workflow_id,
            "llm_provider": self.llm_provider.provider_name,
            "runtime_provider": self.runtime_config.provider,
            "runtime_model": self.runtime_config.model,
            "requested_provider": self.runtime_config.requested_provider,
            "requested_model": self.runtime_config.requested_model,
            "response_schema": schema_name,
            "has_reference_image": bool(image_bytes),
            "image_mime_type": image_mime_type,
        }
        provider_event_details = {
            "provider": self.llm_provider.provider_name,
            "model": self.llm_provider.model_name,
            "runtime_provider": self.runtime_config.provider,
            "runtime_model": self.runtime_config.model,
            "schema": schema_name,
            "has_reference_image": bool(image_bytes),
        }
        with start_observation(
            name=f"forma.{self.workflow_id}.{schema_name}",
            as_type="generation",
            model=self.llm_provider.model_name,
            input={
                "prompt": prompt,
                "schema": schema_name,
                "has_reference_image": bool(image_bytes),
                "image_mime_type": image_mime_type,
            },
            metadata=metadata,
        ) as observation:
            try:
                if pipeline_step_id:
                    emit_agent_pipeline_event(
                        self.workflow_id,
                        pipeline_step_id,
                        "provider_request_started",
                        details=provider_event_details,
                    )
                result = self.llm_provider.generate_structured(prompt, schema_class, image_bytes, image_mime_type)
                self.model_name = self.llm_provider.model_name
                update_observation(
                    observation,
                    output=serialize_for_langfuse(result),
                    metadata={**metadata, "actual_model": self.model_name},
                )
                if pipeline_step_id:
                    emit_agent_pipeline_event(
                        self.workflow_id,
                        pipeline_step_id,
                        "provider_response_received",
                        details={**provider_event_details, "actual_model": self.model_name},
                    )
                return result
            except Exception as exc:
                if pipeline_step_id:
                    emit_agent_pipeline_event(
                        self.workflow_id,
                        pipeline_step_id,
                        "provider_request_failed",
                        details={
                            **provider_event_details,
                            "error_type": exc.__class__.__name__,
                            "error": str(exc)[:500],
                        },
                    )
                update_observation(
                    observation,
                    metadata={**metadata, "error_type": exc.__class__.__name__, "error": str(exc)[:1000]},
                )
                raise

    def generate_project(
        self,
        user_prompt: str,
        image_bytes: Optional[bytes] = None,
        image_mime_type: Optional[str] = None,
        generation_metadata: Optional[Dict[str, Any]] = None,
    ) -> HardwareIntermediateRepresentation:
        self.validate_configured_model()
        self._active_generation_metadata = {
            key: value
            for key, value in (generation_metadata or {}).items()
            if value is not None and value != ""
        }
        self._active_generation_metadata.setdefault(
            "cad_required",
            False,
        )
        emit_agent_pipeline_event(self.workflow_id, "safety_guardrail", "started")
        safety_prompt = str(self._active_generation_metadata.get("project_prompt") or user_prompt)
        safety_error = check_safety_violations(safety_prompt)
        if safety_error:
            emit_agent_pipeline_event(self.workflow_id, "safety_guardrail", "failed", details={"reason": safety_error})
            logger.info("Web research workflow safety guardrail blocked request; delegating to safety response.")
            return HardwarePipelineOrchestrator(runtime_config=self.runtime_config, settings=self.settings).generate_project(
                user_prompt,
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
                generation_metadata=self._active_generation_metadata,
            )

        if self.use_simulation:
            if deployment_mode_enabled():
                raise AlphaGenerationUnavailableError(generation_unavailable_message(self.get_debug_config()))
            logger.info("Web research workflow is using simulation fallback because external generation is unavailable.")
            emit_agent_pipeline_event(self.workflow_id, "external_research", "skipped", details={"reason": self.research_client.config.reason})
            ir = HardwarePipelineOrchestrator(use_simulation=True, runtime_config=self.runtime_config, settings=self.settings).generate_project(
                user_prompt,
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
                generation_metadata=self._active_generation_metadata,
            )
            ir.assembly_metadata = {
                **(ir.assembly_metadata or {}),
                "workflow": self.workflow_id,
                "source_usage": source_usage_for_workflow(
                    self.workflow_id,
                    external_provider=self.research_client.provider_name,
                ),
                "workflow_fallback": "simulation",
                "external_research": {
                    "provider": self.research_client.provider_name,
                    "error": self.research_client.config.reason,
                },
            }
            return ir

        try:
            model_validation = self.validate_configured_model()
            self.model_name = model_validation.actual_model or self.llm_provider.model_name
            if image_bytes:
                self.llm_provider.validate_image_input()
        except LLMProviderConfigError as exc:
            if deployment_mode_enabled():
                raise AlphaGenerationUnavailableError(generation_unavailable_message(self.get_debug_config())) from exc
            raise

        emit_agent_pipeline_event(self.workflow_id, "safety_guardrail", "completed")
        logger.info("Invoking Context Clarifier Agent...")
        with agent_pipeline_step(self.workflow_id, "context_clarifier", details={
            "has_human_context": "HUMAN-IN-THE-LOOP CONTEXT:" in user_prompt,
        }):
            pass
        if self._active_generation_metadata.get("legacy_atomic_generation") is not True:
            return self._generate_staged_project(
                user_prompt,
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
                model_validation=model_validation,
            )
        logger.info("Starting Web Research Pipeline Execution...")
        logger.info("Invoking External Source Research Agent...")
        # Past-job context belongs in the architecture prompts, not in external
        # search queries where it would create oversized or overly specific requests.
        research_prompt = str(self._active_generation_metadata.get("project_prompt") or user_prompt)
        research_queries = self._research_queries(research_prompt)
        with agent_pipeline_step(self.workflow_id, "external_research", details={
            "provider": self.research_client.provider_name,
            "query_count": len(research_queries),
            "timeout_seconds": self.research_client.config.timeout_seconds,
        }):
            research = self._research(research_queries)
            research_context = research.as_prompt_context()

        logger.info("Invoking Web Research Hardware Architect Agent...")
        with agent_pipeline_step(self.workflow_id, "web_architect"):
            plan = self._plan_project(user_prompt, research_context, image_bytes, image_mime_type)
        logger.info("Invoking Web Component Sourcing Agent...")
        with agent_pipeline_step(self.workflow_id, "web_component_sourcing"):
            selection = self._select_components(user_prompt, plan, research_context)
            components = expand_component_instances(selection.components)
            components_json = json.dumps([component_detail_payload(component) for component in components], indent=2)

        logger.info("Invoking Wiring/Netlist Agent...")
        with agent_pipeline_step(self.workflow_id, "wiring_netlist"):
            wiring = self._wire_project(user_prompt, plan, components_json)
            nets = wiring.nets
            pin_mappings = wiring.pin_mappings

        logger.info("Running circuit validation checks on web-researched netlist...")
        with agent_pipeline_step(self.workflow_id, "validation_repair"):
            validation_issues = [
                *wiring.intent_issues,
                *validate_circuit(components, nets, plan.requirements, prompt=user_prompt),
            ]
            if validation_issues:
                emit_agent_pipeline_event(
                    self.workflow_id,
                    "validation_repair",
                    "diagnostics",
                    details={
                        "failure_categories": sorted({
                            wiring_failure_category(issue) for issue in validation_issues
                        }),
                    },
                )
            is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues)
            if not is_valid:
                logger.info("Invoking Validation + Auto-Correction Agent...")
                corrected = self._repair_wiring(plan, components_json, nets, validation_issues)
                nets = corrected.nets
                pin_mappings = corrected.pin_mappings
                validation_issues = [
                    *corrected.intent_issues,
                    *validate_circuit(components, nets, plan.requirements, prompt=user_prompt),
                ]
                is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in validation_issues)

        total_cost = sum(
            component.unit_price * component_instance_count(component)
            for component in components
        )
        plan.overview.estimated_cost = round(total_cost, 2)

        logger.info("Invoking Mechanical/Fabrication Agent...")
        with agent_pipeline_step(self.workflow_id, "mechanical_fabrication"):
            mechanical = self._generate_mechanical(plan, components_json, research_context)
        logger.info("Invoking Assembly Instruction Agent...")
        with agent_pipeline_step(self.workflow_id, "assembly"):
            assembly = self._generate_assembly(plan, components_json, nets, mechanical)

        constraints = plan.requirements.physical_constraints + [f"Operating Voltage: {plan.requirements.operating_voltage}V"]
        fab_notes = mechanical.fabrication_details if mechanical else []
        power_rails = extract_power_rails(components, nets)
        buses = extract_buses(nets)
        current_draw = estimate_current_draw(components)

        logger.info("Invoking Hardware Output Completeness Auditor Agent...")
        with agent_pipeline_step(self.workflow_id, "completeness_audit"):
            audit = self._audit_output(plan, components, nets, mechanical, assembly, validation_issues)
            all_issues = [*validation_issues, *self._audit_to_validation_issues(audit)]

        logger.info("Packaging web research project artifacts...")
        with agent_pipeline_step(self.workflow_id, "package_project"):
            project_ir = HardwareIntermediateRepresentation(
                hardware_ir_version="0.1",
                overview=plan.overview,
                requirements=plan.requirements,
                system_architecture=plan.system_architecture,
                components=components,
                nets=nets,
                buses=buses,
                pin_mappings=pin_mappings,
                assembly=assembly,
                mechanical=mechanical,
                constraints=constraints,
                power_rails=power_rails,
                estimated_current_draw_ma=current_draw,
                fabrication_notes=fab_notes,
                assembly_metadata={
                "generated_at": datetime.utcnow().isoformat(),
                "revision": 1,
                "model_name": self.model_name,
                "fallback_mode": model_validation.fallback_active,
                "requested_model": model_validation.requested_model,
                "actual_model": model_validation.actual_model,
                "llm_provider": model_validation.provider,
                "requested_provider": self.runtime_config.requested_provider or self.runtime_config.provider,
                "runtime_provider": self.runtime_config.provider,
                "runtime_model": self.runtime_config.model,
                "provider_overridden": self.runtime_config.provider_overridden,
                "model_overridden": self.runtime_config.model_overridden,
                "workflow": self.workflow_id,
                "source_usage": source_usage_for_workflow(
                    self.workflow_id,
                    external_provider=research.provider,
                ),
                "pipeline": f"{research.provider.title()} external source research + sourced hardware agents",
                "component_source_policy": "web-sourced components; not constrained to seed_db.py",
                "architecture_notes": plan.architecture_notes,
                "recommended_component_roles": plan.recommended_component_roles,
                "sourcing_notes": selection.sourcing_notes,
                "external_research": research.source_metadata(),
                "firecrawl_research": research.source_metadata() if research.provider == "firecrawl" else None,
                "tavily_research": research.source_metadata() if research.provider == "tavily" else None,
                "completeness_audit": audit.model_dump(),
                "image_features": plan.architecture_notes + plan.recommended_component_roles,
                },
                project_version_history=[
                    {
                        "version": "0.1",
                        "description": f"Initial design compilation via {research.provider} external source research workflow",
                    }
                ],
                validation=build_validation_summary(all_issues),
                is_valid=not any(issue.severity.upper() == "CRITICAL" for issue in all_issues),
            )

            project_ir = build_mechanical_render_data(project_ir)
            self._save_project_to_db(user_prompt, project_ir)
        return project_ir

    def _generate_staged_project(
        self,
        user_prompt: str,
        *,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
        model_validation: LLMProviderValidation,
    ) -> HardwareIntermediateRepresentation:
        """Run artifact-producing stages independently and checkpoint each result."""

        metadata = self._active_generation_metadata
        metadata["project_id"] = canonical_project_uuid(metadata.get("project_id"))
        prior_run = metadata.get("prior_generation_run")
        prior_run = prior_run if isinstance(prior_run, dict) else {}

        def persist_stage_run(stage_run: GenerationStageRun, _record: Any = None) -> None:
            snapshot = self._project_ir_from_stage_run(
                stage_run,
                user_prompt=user_prompt,
                model_validation=model_validation,
            )
            if not self._save_project_to_db(user_prompt, snapshot):
                raise RuntimeError("Generation stage checkpoint could not be persisted.")

        stage_run = GenerationStageRun(
            self.workflow_id,
            WEB_GENERATION_STAGE_SPECS,
            run_id=prior_run.get("generation_run_id") or metadata.get("frontend_job_id"),
            prior_records=prior_run.get("records") if isinstance(prior_run.get("records"), dict) else None,
            retry_stage=metadata.get("retry_stage"),
            replay_retry=bool(metadata.get("retry_stage_replay")),
            persist=persist_stage_run,
        )
        # The project and generation run become durable before the first costly call.
        stage_run.checkpoint()

        research_prompt = str(metadata.get("project_prompt") or user_prompt)
        research_queries = self._research_queries(research_prompt)
        research = stage_run.run(
            "external_research",
            lambda: self._research(research_queries),
            schema=ExternalSourceLibrary,
        )
        research_context = research.as_prompt_context() if research is not None else ""

        plan = stage_run.run(
            "web_architect",
            lambda: self._plan_project(
                user_prompt,
                research_context,
                image_bytes,
                image_mime_type,
            ),
            schema=WebProjectPlan,
        )
        selection = stage_run.run(
            "web_component_sourcing",
            lambda: self._select_components(user_prompt, plan, research_context),
            schema=WebComponentSelection,
        )
        components = expand_component_instances(selection.components) if selection is not None else []
        components_json = json.dumps(
            [component_detail_payload(component) for component in components],
            indent=2,
        )
        wiring = stage_run.run(
            "wiring_netlist",
            lambda: self._wire_project(user_prompt, plan, components_json),
            schema=WiringWrapper,
        )

        def validate_and_repair() -> ValidationStageOutput:
            nets = list(wiring.nets)
            pin_mappings = list(wiring.pin_mappings)
            issues = [
                *wiring.intent_issues,
                *validate_circuit(components, nets, plan.requirements, prompt=user_prompt),
            ]
            if issues:
                emit_agent_pipeline_event(
                    self.workflow_id,
                    "validation_repair",
                    "diagnostics",
                    details={
                        "failure_categories": sorted({
                            wiring_failure_category(issue) for issue in issues
                        }),
                    },
                )
            is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in issues)
            if not is_valid:
                corrected = self._repair_wiring(plan, components_json, nets, issues)
                nets = corrected.nets
                pin_mappings = corrected.pin_mappings
                issues = [
                    *corrected.intent_issues,
                    *validate_circuit(components, nets, plan.requirements, prompt=user_prompt),
                ]
                is_valid = not any(issue.severity.upper() == "CRITICAL" for issue in issues)
            return ValidationStageOutput(
                nets=nets,
                pin_mappings=pin_mappings,
                issues=issues,
                is_valid=is_valid,
            )

        validation = stage_run.run(
            "validation_repair",
            validate_and_repair,
            schema=ValidationStageOutput,
        )
        mechanical = stage_run.run(
            "mechanical_fabrication",
            lambda: self._generate_mechanical(plan, components_json, research_context),
            schema=MechanicalNotes,
        )
        assembly_wrapper = stage_run.run(
            "assembly",
            lambda: AssemblyWrapper(steps=self._generate_assembly(
                plan,
                components_json,
                list(validation.nets if validation is not None else wiring.nets),
                mechanical,
            )),
            schema=AssemblyWrapper,
        )
        assembly = assembly_wrapper.steps if assembly_wrapper is not None else []
        validation_issues = validation.issues if validation is not None else []
        stage_run.run(
            "completeness_audit",
            lambda: self._audit_output(
                plan,
                components,
                list(validation.nets),
                mechanical,
                assembly,
                validation_issues,
            ),
            schema=CompletenessAudit,
        )
        cad_snapshot = self._project_ir_from_stage_run(
            stage_run,
            user_prompt=user_prompt,
            model_validation=model_validation,
        )
        stage_run.run(
            "cad_generation",
            lambda: self._generate_cad_stage(cad_snapshot, metadata),
        )
        stage_run.run(
            "package_project",
            lambda: {
                "project_id": metadata["project_id"],
                "project_readiness": self._project_readiness(stage_run),
            },
        )
        return self._project_ir_from_stage_run(
            stage_run,
            user_prompt=user_prompt,
            model_validation=model_validation,
        )

    def _generate_cad_stage(
        self,
        project: HardwareIntermediateRepresentation,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        ensure_native_cad_model(
            project,
            project_id=metadata.get("project_id"),
            required=bool(
                metadata.get(
                    "cad_required",
                    False,
                )
            ),
        )
        return {
            "cad_model": project.cad_model,
            "cad_generation": (project.assembly_metadata or {}).get("cad_generation"),
        }

    def _project_ir_from_stage_run(
        self,
        stage_run: GenerationStageRun,
        *,
        user_prompt: str,
        model_validation: LLMProviderValidation,
    ) -> HardwareIntermediateRepresentation:
        plan = stage_run.output("web_architect", WebProjectPlan)
        selection = stage_run.output("web_component_sourcing", WebComponentSelection)
        components = expand_component_instances(selection.components) if selection is not None else []
        wiring = stage_run.output("wiring_netlist", WiringWrapper)
        validation = stage_run.output("validation_repair", ValidationStageOutput)
        mechanical = stage_run.output("mechanical_fabrication", MechanicalNotes)
        assembly_wrapper = stage_run.output("assembly", AssemblyWrapper)
        audit = stage_run.output("completeness_audit", CompletenessAudit)
        cad_output = stage_run.output("cad_generation") or {}
        research = stage_run.output("external_research", ExternalSourceLibrary)

        nets = list(validation.nets if validation is not None else (wiring.nets if wiring is not None else []))
        pin_mappings = derive_pin_mappings(components, nets)
        validation_issues = list(validation.issues if validation is not None else [])
        all_issues = [*validation_issues, *(self._audit_to_validation_issues(audit) if audit is not None else [])]
        assembly = list(assembly_wrapper.steps if assembly_wrapper is not None else [])
        constraints = []
        if plan is not None:
            constraints = [
                *plan.requirements.physical_constraints,
                f"Operating Voltage: {plan.requirements.operating_voltage}V",
            ]
            plan.overview.estimated_cost = round(
                sum(component.unit_price * component_instance_count(component) for component in components),
                2,
            )

        generation_status = self._generation_status(stage_run)
        cad_record = stage_run.records.get("cad_generation")
        if (
            self._active_generation_metadata.get("cad_required") is True
            and cad_record is not None
            and cad_record.status.value == "failed"
        ):
            generation_status = "failed"
            all_issues.append(ValidationIssue(
                severity="CRITICAL",
                category="CAD Generation Failure",
                description=str((cad_record.error or {}).get("message") or "Native CAD generation failed."),
                troubleshooting="Install the OpenCAD runtime/adapter or retry native CAD generation.",
            ))
        stage_snapshot = stage_run.snapshot(include_outputs=True)
        public_generation_metadata = {
            key: value
            for key, value in self._active_generation_metadata.items()
            if key not in {"owner_user_id", "project_prompt", "prior_generation_run"}
        }
        failed_stages = [
            record.model_dump(mode="json", exclude={"output"})
            for record in stage_run.records.values()
            if record.status.value in {"failed", "blocked"}
        ]
        project_ir = HardwareIntermediateRepresentation(
            overview=plan.overview if plan is not None else None,
            requirements=plan.requirements if plan is not None else None,
            system_architecture=plan.system_architecture if plan is not None else None,
            components=components,
            nets=nets,
            buses=extract_buses(nets),
            pin_mappings=pin_mappings,
            assembly=assembly,
            mechanical=mechanical,
            cad_model=cad_output.get("cad_model") if isinstance(cad_output, dict) else None,
            constraints=constraints,
            power_rails=extract_power_rails(components, nets),
            estimated_current_draw_ma=estimate_current_draw(components),
            fabrication_notes=mechanical.fabrication_details if mechanical is not None else [],
            assembly_metadata={
                **public_generation_metadata,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "revision": 1,
                "model_name": self.model_name,
                "fallback_mode": model_validation.fallback_active,
                "requested_model": model_validation.requested_model,
                "actual_model": model_validation.actual_model,
                "llm_provider": model_validation.provider,
                "runtime_provider": self.runtime_config.provider,
                "runtime_model": self.runtime_config.model,
                "workflow": self.workflow_id,
                "generation_status": generation_status,
                "project_readiness": self._project_readiness(stage_run),
                "generation_run": stage_snapshot,
                "generation_stage_failures": failed_stages,
                "source_usage": source_usage_for_workflow(
                    self.workflow_id,
                    external_provider=research.provider if research is not None else self.research_client.provider_name,
                ),
                "external_research": research.source_metadata() if research is not None else None,
                "sourcing_notes": selection.sourcing_notes if selection is not None else [],
                "completeness_audit": audit.model_dump(mode="json") if audit is not None else None,
                "cad_generation": (
                    cad_output.get("cad_generation") if isinstance(cad_output, dict) else None
                ),
            },
            project_version_history=[{
                "version": "0.2",
                "description": "Dependency-aware staged web research generation",
            }],
            validation=build_validation_summary(all_issues),
            is_valid=bool(validation is not None and validation.is_valid and not any(
                issue.severity.upper() == "CRITICAL" for issue in all_issues
            )),
        )
        if mechanical is not None:
            project_ir = build_mechanical_render_data(project_ir)
        return project_ir

    @staticmethod
    def _generation_status(stage_run: GenerationStageRun) -> str:
        architecture = stage_run.records["web_architect"].status.value
        if architecture in {"failed", "blocked"}:
            return "failed"
        return stage_run.overall_status

    @staticmethod
    def _project_readiness(stage_run: GenerationStageRun) -> str:
        statuses = {stage_id: record.status.value for stage_id, record in stage_run.records.items()}
        if statuses.get("web_architect") != "succeeded":
            return "draft"
        if statuses.get("cad_generation") in {"failed", "blocked"}:
            return "partial"
        non_package_statuses = [
            status for stage_id, status in statuses.items()
            if stage_id != "package_project"
        ]
        package_status = statuses.get("package_project")
        if (
            non_package_statuses
            and all(status == "succeeded" for status in non_package_statuses)
            and package_status in {"running", "succeeded"}
        ):
            return "complete"
        if all(statuses.get(stage_id) == "succeeded" for stage_id in (
            "web_architect",
            "web_component_sourcing",
            "wiring_netlist",
            "validation_repair",
        )):
            return "core_ready"
        if any(status == "succeeded" for status in statuses.values()) and any(
            status in {"failed", "blocked"} for status in statuses.values()
        ):
            return "partial"
        return "draft"

    def _research_queries(self, user_prompt: str) -> List[str]:
        return [
            f"{user_prompt} open source hardware schematic BOM",
            f"{user_prompt} maker project components wiring datasheet",
            f"{user_prompt} Arduino ESP32 module component reference design",
        ]

    def _research(self, queries: List[str]) -> ExternalSourceLibrary:
        emit_agent_pipeline_event(
            self.workflow_id,
            "external_research",
            "provider_request_started",
            details={
                "provider": self.research_client.provider_name,
                "query_count": len(queries),
                "timeout_seconds": self.research_client.config.timeout_seconds,
            },
        )
        research = self.research_client.research(queries)
        emit_agent_pipeline_event(
            self.workflow_id,
            "external_research",
            "provider_response_received" if not research.error else "provider_response_failed",
            details={
                "provider": research.provider,
                "configured": research.configured,
                "searches_attempted": research.searches_attempted,
                "source_count": len(research.sources),
                "error": research.error,
            },
        )
        return research

    def _plan_project(
        self,
        user_prompt: str,
        research_context: str,
        image_bytes: Optional[bytes],
        image_mime_type: Optional[str],
    ) -> WebProjectPlan:
        prompt = f"""
        You are a Web Research Hardware Architect Agent.
        Turn the user request into a buildable low-voltage maker electronics architecture.

        User request:
        {user_prompt}

        External source research context:
        {research_context}

        Return WebProjectPlan. Prefer concrete component roles that are supported by the research context.
        Build system_architecture as a purpose-driven hierarchy with a product root and applicable electrical,
        mechanical, and firmware branches. Include nested systems such as electrical.power and
        mechanical.enclosure. Keep this tree free of exact parts, nets, and pins; specialists add those later.
        Keep the design in safe low-voltage DC maker-electronics scope.
        """
        return self._call_llm_structured(prompt, WebProjectPlan, image_bytes, image_mime_type, pipeline_step_id="web_architect")

    def _select_components(
        self,
        user_prompt: str,
        plan: WebProjectPlan,
        research_context: str,
    ) -> WebComponentSelection:
        prompt = f"""
        You are a Web Component Sourcing Agent.
        Select real, buildable components for this project using external source research and common datasheet-backed maker hardware.

        User request:
        {user_prompt}

        Project plan:
        {plan.model_dump_json()}

        External source research context:
        {research_context}

        Important rules:
        - Do not constrain yourself to the local seed database.
        - Use real components or modules that a maker could plausibly buy.
        - Prefer source URLs from the research context. Leave sourcing_url null if the URL is unknown; do not invent URLs.
        - Include one microcontroller or SBC unless the project clearly does not need compute.
        - Include a realistic low-voltage power source/regulator path.
        - Include complete relevant pins for each selected component: power, ground, interfaces, control, analog, and outputs.
        - Give each project instance a unique ref_des such as U1, SEN1, DIS1, PWR1, REG1, ACT1, SW1, R1.
        - Every ComponentInstance is one physical occurrence. Emit repeated parts as separate records with unique
          reference designators (for example M1, M2, M3, and M4); never use aggregate quantity semantics.
        - For complex boards, include the pins needed for this build rather than every package pin.

        Return WebComponentSelection.
        """
        return self._call_llm_structured(prompt, WebComponentSelection, pipeline_step_id="web_component_sourcing")

    def _wire_project(
        self,
        user_prompt: str,
        plan: WebProjectPlan,
        components_json: str,
    ) -> WiringWrapper:
        components = [ComponentInstance.model_validate(item) for item in json.loads(components_json)]
        endpoint_catalog = endpoint_catalog_prompt(build_endpoint_catalog(components))
        prompt = f"""
        You are a Wiring Intent Agent for sourced web components.
        Describe safe low-voltage connections using only exact endpoint IDs from the catalog.
        Do not create canonical net IDs, nested ref_des/pin_id objects, or pin mappings.

        User request:
        {user_prompt}

        Requirements:
        {plan.requirements.model_dump_json()}

        Endpoint catalog:
        {json.dumps(endpoint_catalog, indent=2)}

        Rules:
        - Every power endpoint must connect to a compatible source or regulated-output rail.
        - Every ground endpoint must connect to a ground net.
        - Do not short power to ground or mix incompatible logic voltages.
        - Use level shifting or voltage-compatible parts when needed.
        - A non-power/non-ground endpoint must appear in only one net.
        - Passive components bridge nets with one passive endpoint per net.

        Return WiringIntent with name, net_type, voltage, and endpoint_ids for each net.
        """
        wiring_intent: WiringIntent = self._call_llm_structured(
            prompt,
            WiringIntent,
            pipeline_step_id="wiring_netlist",
        )
        compilation = compile_wiring_intent(components, wiring_intent)
        return WiringWrapper(
            nets=compilation.nets,
            pin_mappings=derive_pin_mappings(components, compilation.nets),
            intent_issues=compilation.issues,
        )

    def _repair_wiring(
        self,
        plan: WebProjectPlan,
        components_json: str,
        nets: List[ConnectionNet],
        issues: List[ValidationIssue],
    ) -> WiringWrapper:
        components = [ComponentInstance.model_validate(item) for item in json.loads(components_json)]
        prompt = f"""
        You are a Wiring Intent Auto-Correction Agent.
        Correct only the rejected or invalid nets using the validation report.
        Return replacement intents for existing canonical nets using replace_net_id. Omit valid nets.

        Requirements:
        {plan.requirements.model_dump_json()}

        Endpoint catalog:
        {json.dumps(endpoint_catalog_prompt(build_endpoint_catalog(components)), indent=2)}

        Previous nets:
        {json.dumps([net.model_dump() for net in nets], indent=2)}

        Validation issues:
        {json.dumps([issue.model_dump() for issue in issues], indent=2)}

        Use endpoint_ids from the catalog only. Keep valid existing nets unchanged. Return WiringIntent.
        """
        repair_intent: WiringIntent = self._call_llm_structured(
            prompt,
            WiringIntent,
            pipeline_step_id="validation_repair",
        )
        compilation = compile_wiring_intent(components, repair_intent, existing_nets=nets)
        merged_nets = compilation.all_nets
        return WiringWrapper(
            nets=merged_nets,
            pin_mappings=derive_pin_mappings(components, merged_nets),
            intent_issues=compilation.issues,
        )

    def _generate_mechanical(
        self,
        plan: WebProjectPlan,
        components_json: str,
        research_context: str,
    ) -> MechanicalNotes:
        mechanical_context = system_context(plan.system_architecture, "mechanical")
        components = [ComponentInstance.model_validate(item) for item in json.loads(components_json)]
        prompt = f"""
        You are a Mechanical/Fabrication and CAD Sourcing Agent.
        Produce enclosure, mounting, fabrication, CAD source, and 3D render placement details.

        Project overview and requirements:
        {json.dumps({"overview": plan.overview.model_dump(), "requirements": plan.requirements.model_dump()}, indent=2)}

        Mechanical system branch:
        {json.dumps(mechanical_context, indent=2)}

        Components (pin definitions intentionally omitted):
        {json.dumps(compact_component_context(components), indent=2)}

        Research context:
        {research_context}

        Populate physical_form with the requested overall shape, silhouette, and form factor. Treat explicit human shape context as authoritative and do not default to a rectangular project box. If the project is exposed, structural, or open-frame, do not invent a closed case.
        For a requested two-gear motion example, use mechanism_benchmark with kind spur_gear_pair. Set driver_teeth and driven_teeth (defaults 20/40), module_mm (default 2), face_width_mm (default 8), and cycle_seconds (default 6) to match the request. OpenCAD generates separate gear bodies and coupled motion; do not substitute boxes or independent motion_intents.
        Use CAD/enclosure URLs only when present in research or well-known source data. If no source exists, keep cad_sources empty.
        Return MechanicalNotes.
        """
        return self._call_llm_structured(prompt, MechanicalNotes, pipeline_step_id="mechanical_fabrication")

    def _generate_assembly(
        self,
        plan: WebProjectPlan,
        components_json: str,
        nets: List[ConnectionNet],
        mechanical: MechanicalNotes,
    ) -> List[AssemblyStep]:
        components = [ComponentInstance.model_validate(item) for item in json.loads(components_json)]
        prompt = f"""
        You are an Assembly Instruction Agent.
        Produce concrete step-by-step build instructions.

        Project:
        {plan.overview.model_dump_json()}

        System hierarchy:
        {json.dumps(system_context(plan.system_architecture), indent=2)}

        Components (pin definitions intentionally omitted):
        {json.dumps(compact_component_context(components), indent=2)}

        Nets (system connectivity without individual pin IDs):
        {json.dumps(compact_net_context(nets), indent=2)}

        Mechanical guide:
        {mechanical.model_dump_json()}

        Mention safety flags for batteries, motors, relays, soldering, heat, moving parts, and polarity.
        Return AssemblyWrapper.
        """
        wrapper: AssemblyWrapper = self._call_llm_structured(prompt, AssemblyWrapper, pipeline_step_id="assembly")
        return wrapper.steps

    def _audit_output(
        self,
        plan: WebProjectPlan,
        components: List[ComponentInstance],
        nets: List[ConnectionNet],
        mechanical: MechanicalNotes,
        assembly: List[AssemblyStep],
        validation_issues: List[ValidationIssue],
    ) -> CompletenessAudit:
        deterministic_missing = self._deterministic_missing_checks(components, nets, mechanical, assembly)
        prompt = f"""
        You are a Hardware Output Completeness Auditor Agent.
        Check whether this generated Hardware IR is likely missing anything important.
        Do not rewrite the design. Only audit it.

        Requirements:
        {plan.requirements.model_dump_json()}

        Components:
        {json.dumps([component_detail_payload(component) for component in components], indent=2)}

        Nets:
        {json.dumps([net.model_dump() for net in nets], indent=2)}

        Mechanical:
        {mechanical.model_dump_json()}

        Assembly steps:
        {json.dumps([step.model_dump() for step in assembly], indent=2)}

        Validation issues:
        {json.dumps([issue.model_dump() for issue in validation_issues], indent=2)}

        Deterministic missing checks already found:
        {json.dumps(deterministic_missing, indent=2)}

        Look for missing power conversion, protection parts, required sensors, connectors, level shifting,
        pullups, current limiting, mounting details, firmware/programming notes, and unclear sourcing.
        Return CompletenessAudit.
        """
        try:
            audit: CompletenessAudit = self._call_llm_structured(prompt, CompletenessAudit, pipeline_step_id="completeness_audit")
        except Exception as exc:
            logger.warning("Completeness audit LLM call failed: %s", exc)
            audit = CompletenessAudit(
                completeness_score=0.0 if deterministic_missing else 0.65,
                missing_items=deterministic_missing,
                possible_risks=[],
                recommended_next_checks=[],
                summary="Deterministic completeness checks ran; LLM audit was unavailable.",
            )

        audit.missing_items = [*deterministic_missing, *audit.missing_items]
        return audit

    def _deterministic_missing_checks(
        self,
        components: List[ComponentInstance],
        nets: List[ConnectionNet],
        mechanical: MechanicalNotes,
        assembly: List[AssemblyStep],
    ) -> List[str]:
        missing: List[str] = []
        categories = {component.category.lower() for component in components}
        if not any("microcontroller" in category or "sbc" in category for category in categories):
            missing.append("No controller-class component is present.")
        if not any("power" in category or "battery" in category or "regulator" in category for category in categories):
            missing.append("No explicit power source or regulator component is present.")
        if not nets:
            missing.append("No electrical nets were generated.")
        if not any(net.net_type.lower() == "ground" for net in nets):
            missing.append("No ground net was generated.")
        if not any(net.net_type.lower() == "power" for net in nets):
            missing.append("No power rail net was generated.")
        if any(not component.pins for component in components):
            missing.append("One or more components have no pin definitions.")
        if not assembly:
            missing.append("No assembly steps were generated.")
        if not mechanical.fabrication_details:
            missing.append("Mechanical fabrication details are sparse.")
        return missing

    def _audit_to_validation_issues(self, audit: CompletenessAudit) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        for item in audit.missing_items:
            issues.append(
                ValidationIssue(
                    severity="WARNING",
                    category="Completeness Audit",
                    description=item,
                    troubleshooting="Review the generated design and add or source this item before building.",
                )
            )
        for risk in audit.possible_risks:
            issues.append(
                ValidationIssue(
                    severity="INFO",
                    category="Completeness Audit",
                    description=risk,
                    troubleshooting="Verify this detail against datasheets and real hardware constraints.",
                )
            )
        return issues

    def _save_project_to_db(self, prompt: str, ir: HardwareIntermediateRepresentation) -> str:
        ensure_agent_pipeline_active()
        project_id = canonical_project_uuid((ir.assembly_metadata or {}).get("project_id"))
        generation_metadata = self._active_generation_metadata or {}
        public_generation_metadata = {
            key: value
            for key, value in generation_metadata.items()
            if key not in {"owner_user_id", "project_prompt", "prior_generation_run"}
        }
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            **public_generation_metadata,
            "project_id": project_id,
        }
        if not self.persist_project:
            return project_id
        try:
            owner_user_id = generation_metadata.get("owner_user_id")
            if owner_user_id:
                persist_chat_project_revision(
                    project_id,
                    owner_user_id,
                    ir,
                    source_job_id=str(generation_metadata.get("frontend_job_id") or f"web-research-{uuid4().hex}"),
                    prompt=str(generation_metadata.get("project_prompt") or prompt),
                    chat_id=generation_metadata.get("chat_id"),
                )
            else:
                persist_legacy_project_projection(
                    project_id=project_id,
                    title=ir.overview.title if ir.overview else "Untitled Forma Project",
                    prompt=str(generation_metadata.get("project_prompt") or prompt),
                    hardware_ir=ir.model_dump(),
                    created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    chat_id=generation_metadata.get("chat_id"),
                    owner_user_id=None,
                    visibility="public",
                )
            try:
                ensure_agent_pipeline_active()
            except PipelineCancelledError:
                owner_user_id = generation_metadata.get("owner_user_id")
                if owner_user_id:
                    update_project_deletion_state(
                        project_id,
                        owner_user_id=owner_user_id,
                        allowed_statuses=["active"],
                        updates={
                            "status": "deletion_pending",
                            "deleted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "deletion_requested_by": owner_user_id,
                        },
                    )
                raise
            logger.info("Web research workflow project saved to database with ID: %s", project_id)
            return project_id
        except PipelineCancelledError:
            raise
        except Exception as exc:
            logger.error("Failed to save web research workflow project: %s", exc)
            return ""
