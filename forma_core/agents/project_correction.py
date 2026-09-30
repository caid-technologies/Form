from __future__ import annotations

from typing import Any, Dict, Optional

from forma_core.validation import validate_circuit
from forma_core.workspaces.projects.iteration import (
    ProjectIterator,
    ProjectSelfCorrectionPlan,
    _dedupe_validation_issues,
    _metadata_output_findings,
    _stored_validation_issues,
    coerce_hardware_ir,
)
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.objects import normalize_project_namespace


class ProjectSelfCorrectionAgent:
    """Plan and apply validation-driven project corrections."""

    def __init__(self, iterator: Optional[ProjectIterator] = None, **iterator_kwargs: Any) -> None:
        self.iterator = iterator or ProjectIterator(**iterator_kwargs)

    def plan_correction(
        self,
        current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
        *,
        target_namespace: Optional[str] = None,
    ) -> ProjectSelfCorrectionPlan:
        ir = coerce_hardware_ir(current_ir)
        issues = _dedupe_validation_issues([*validate_circuit(ir.components, ir.nets), *_stored_validation_issues(ir)])
        critical = [issue for issue in issues if issue.severity.upper() == "CRITICAL"]
        warnings = [issue for issue in issues if issue.severity.upper() == "WARNING"]
        output_findings = _metadata_output_findings(ir)
        normalized_namespace = normalize_project_namespace(target_namespace)
        if normalized_namespace is None:
            normalized_namespace = "product.electrical" if issues else "project.docs"

        if issues or output_findings:
            issue_lines = [
                f"- {issue.severity} {issue.category}: {issue.description} Remediation: {issue.troubleshooting}"
                for issue in issues[:8]
            ]
            output_lines = [f"- Metadata/output: {finding}" for finding in output_findings[:8]]
            instruction = (
                "Self-correct the project by resolving these validation and output/metadata issues while preserving the user's intent.\n"
                "Make the smallest coherent mutation that improves the current revision. If an external service failed, do not fabricate sources; "
                "record the limitation clearly in project docs/history and remove unsupported source claims.\n"
                + "\n".join([*issue_lines, *output_lines])
            )
        else:
            instruction = (
                "Self-review this project namespace, metadata, and generated outputs for consistency. Preserve the current design unless a small "
                "correction is needed to keep the HardwareIntermediateRepresentation internally coherent."
            )

        return ProjectSelfCorrectionPlan(
            target_namespace=normalized_namespace,
            instruction=instruction,
            critical_issue_count=len(critical),
            warning_issue_count=len(warnings),
            output_issue_count=len(output_findings),
        )

    def correct_project(
        self,
        current_ir: HardwareIntermediateRepresentation | Dict[str, Any],
        *,
        original_prompt: Optional[str] = None,
        project_id: Optional[str] = None,
        target_namespace: Optional[str] = None,
    ) -> HardwareIntermediateRepresentation:
        plan = self.plan_correction(current_ir, target_namespace=target_namespace)
        return self.iterator.iterate_project(
            current_ir,
            plan.instruction,
            original_prompt=original_prompt,
            project_id=project_id,
            target_namespace=plan.target_namespace,
        )


__all__ = ["ProjectSelfCorrectionAgent"]
