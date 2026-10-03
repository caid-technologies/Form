"""Deterministic, domain-scoped context sharing for project objects.

Project namespaces are the source of truth for ownership. This module adds the
policy boundary around those namespaces so an agent receives an explicit
projection instead of the complete project object.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CONTEXT_GOVERNANCE_SCHEMA_VERSION = "1.0"
ContextShareMode = Literal["none", "summary", "full"]
_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$")


def _normalize_namespace(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _NAMESPACE_PATTERN.fullmatch(normalized):
        raise ValueError("Context namespaces must be dotted, for example product.mech.")
    return normalized


class ContextShareRule(BaseModel):
    """One explicit source-to-recipient sharing decision.

    ``allowed_attributes`` is an allowlist. An empty field list means the
    complete attribute value; it does not mean all attributes are allowed.
    """

    model_config = ConfigDict(extra="forbid")

    source_namespace: str
    recipient_namespace: str
    mode: ContextShareMode = "summary"
    allowed_attributes: dict[str, list[str]] = Field(default_factory=dict)
    reason: str = Field(..., min_length=1)

    @field_validator("source_namespace", "recipient_namespace", mode="before")
    @classmethod
    def normalize_namespaces(cls, value: str) -> str:
        return _normalize_namespace(value)

    @field_validator("allowed_attributes", mode="before")
    @classmethod
    def normalize_attribute_allowlist(cls, value: Any) -> dict[str, list[str]]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("allowed_attributes must be an object of attribute names to field names.")
        normalized: dict[str, list[str]] = {}
        for attribute, fields in value.items():
            name = str(attribute or "").strip()
            if not name:
                raise ValueError("Context attribute names must not be empty.")
            if fields is None:
                normalized[name] = []
                continue
            if isinstance(fields, str):
                fields = [fields]
            if not isinstance(fields, list):
                raise ValueError(f"Context fields for '{name}' must be a list.")
            normalized[name] = [str(field).strip() for field in fields if str(field).strip()]
        return normalized

    @model_validator(mode="after")
    def require_grant_for_shared_mode(self) -> "ContextShareRule":
        if self.mode != "none" and not self.allowed_attributes:
            raise ValueError("A shared context rule must declare allowed_attributes.")
        return self


class ContextGovernancePolicy(BaseModel):
    """Versioned allowlist for context handoffs between project agents."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = CONTEXT_GOVERNANCE_SCHEMA_VERSION
    default_mode: Literal["none"] = "none"
    rules: list[ContextShareRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_unique_rules(self) -> "ContextGovernancePolicy":
        keys = [(rule.source_namespace, rule.recipient_namespace) for rule in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("Context governance rules must have unique source and recipient namespaces.")
        return self

    def rule_for(self, source_namespace: str, recipient_namespace: str) -> ContextShareRule | None:
        source = _normalize_namespace(source_namespace)
        recipient = _normalize_namespace(recipient_namespace)
        return next(
            (
                rule
                for rule in self.rules
                if rule.source_namespace == source and rule.recipient_namespace == recipient
            ),
            None,
        )

    def recipient_manifest(self, recipient_namespace: str) -> dict[str, Any]:
        recipient = _normalize_namespace(recipient_namespace)
        return {
            "schema_version": self.schema_version,
            "recipient_namespace": recipient,
            "default_mode": self.default_mode,
            "same_namespace_access": "full",
            "grants": {
                rule.source_namespace: {
                    "mode": rule.mode,
                    "allowed_attributes": deepcopy(rule.allowed_attributes),
                    "reason": rule.reason,
                }
                for rule in self.rules
                if rule.recipient_namespace == recipient and rule.mode != "none"
            },
        }


class ContextProjection(BaseModel):
    """Auditable context passed to one recipient agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = CONTEXT_GOVERNANCE_SCHEMA_VERSION
    object_id: str
    object_version: int = Field(ge=1)
    recipient_namespace: str
    namespaces: dict[str, dict[str, Any]] = Field(default_factory=dict)
    allowed_sources: list[str] = Field(default_factory=list)
    denied_sources: list[str] = Field(default_factory=list)
    omitted_attributes: dict[str, list[str]] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


def project_context_for_agent(
    project_object: Any,
    recipient_namespace: str,
    *,
    policy: ContextGovernancePolicy | None = None,
) -> dict[str, Any]:
    """Return only context permitted for ``recipient_namespace``.

    The result includes a receipt so downstream workers can record which
    namespaces were allowed or denied. Unknown recipients and missing rules
    are denied; no prompt or model call is involved in this decision.
    """

    active_policy = policy if policy is not None else DEFAULT_CONTEXT_GOVERNANCE_POLICY
    recipient = _normalize_namespace(recipient_namespace)
    object_id = str(_read(project_object, "object_id") or "")
    object_version = _positive_int(_read(project_object, "version"), default=1)
    raw_namespaces = _read(project_object, "namespaces") or []

    projected: dict[str, dict[str, Any]] = {}
    allowed_sources: list[str] = []
    denied_sources: list[str] = []
    omitted_attributes: dict[str, list[str]] = {}
    for raw_namespace in raw_namespaces:
        source = str(_read(raw_namespace, "name") or "").strip().lower()
        if not source:
            continue
        payload = _read(raw_namespace, "payload") or {}
        if not isinstance(payload, Mapping):
            payload = {}

        if source == recipient:
            mode = "full"
            allowed_attributes = {str(key): [] for key in payload}
        else:
            rule = active_policy.rule_for(source, recipient)
            mode = rule.mode if rule else active_policy.default_mode
            allowed_attributes = rule.allowed_attributes if rule else {}

        if mode == "none":
            denied_sources.append(source)
            omitted_attributes[source] = sorted(str(attribute) for attribute in payload)
            continue

        selected: dict[str, Any] = {}
        omitted: list[str] = []
        for attribute, fields in allowed_attributes.items():
            if attribute not in payload:
                continue
            if _is_secret_key(attribute):
                omitted.append(attribute)
                continue
            selected[attribute] = _select_fields(payload[attribute], fields)
            omitted.extend(_omitted_paths(attribute, payload[attribute], fields))
        omitted.extend(str(attribute) for attribute in payload if attribute not in allowed_attributes)
        if omitted:
            omitted_attributes[source] = sorted(set(omitted))
        if selected:
            projected[source] = selected
            allowed_sources.append(source)
        else:
            denied_sources.append(source)

    projection = ContextProjection(
        object_id=object_id,
        object_version=object_version,
        recipient_namespace=recipient,
        namespaces=projected,
        allowed_sources=sorted(set(allowed_sources)),
        denied_sources=sorted(set(denied_sources)),
        omitted_attributes=omitted_attributes,
        policy=active_policy.recipient_manifest(recipient),
    )
    return projection.model_dump(mode="json")


def _read(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _select_fields(value: Any, fields: list[str]) -> Any:
    if not fields:
        return _sanitize(value)
    allowed = set(fields)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if str(key) in allowed and not _is_secret_key(key)
        }
    if isinstance(value, list):
        return [
            {
                str(key): _sanitize(item)
                for key, item in item_value.items()
                if str(key) in allowed and not _is_secret_key(key)
            }
            if isinstance(item_value, Mapping)
            else _sanitize(item_value)
            for item_value in value
        ]
    return _sanitize(value)


def _omitted_paths(prefix: str, value: Any, fields: list[str]) -> list[str]:
    if not fields:
        return []
    allowed = set(fields)
    if isinstance(value, Mapping):
        return [
            f"{prefix}.{key}"
            for key in value
            if str(key) not in allowed or _is_secret_key(key)
        ]
    if isinstance(value, list):
        paths: set[str] = set()
        for item in value:
            if isinstance(item, Mapping):
                paths.update(_omitted_paths(f"{prefix}[]", item, fields))
        return sorted(paths)
    return []


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _is_secret_key(key)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return f"<redacted inline data: {len(value)} chars>"
    return deepcopy(value)


def _is_secret_key(value: Any) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return normalized in {"key", "credential", "credentials"} or any(
        marker in normalized
        for marker in ("api_key", "apikey", "authorization", "password", "secret", "token")
    )


def _rule(source: str, recipient: str, attributes: dict[str, list[str]], reason: str) -> ContextShareRule:
    return ContextShareRule(
        source_namespace=source,
        recipient_namespace=recipient,
        allowed_attributes=attributes,
        reason=reason,
    )


_SUMMARY_OVERVIEW = {
    "overview": ["title", "description", "difficulty", "estimated_cost", "category"],
    "requirements": ["requirements", "power_needs", "operating_voltage", "physical_constraints", "safety_notes"],
    "constraints": [],
}
_COMPONENT_SUMMARY = {
    "components": ["ref_des", "part_definition_id", "part_number", "name", "category", "rationale", "configuration"]
}
_NET_SUMMARY = {"nets": ["net_id", "name", "net_type", "voltage", "component_refs"]}


def _default_rules() -> list[ContextShareRule]:
    domain_recipients = (
        "product.architecture",
        "product.electrical",
        "product.bom",
        "product.mech",
        "product.firmware",
        "product.visuals",
        "product.validation",
        "product.assembly",
    )
    rules = [
        _rule("product.overview", recipient, _SUMMARY_OVERVIEW, "Product intent and constraints are shared with every domain specialist.")
        for recipient in domain_recipients
    ]
    rules.extend(
        _rule("product.architecture", recipient, {"system_architecture": []}, "The architecture agent routes the shared system hierarchy.")
        for recipient in domain_recipients
        if recipient != "product.architecture"
    )
    rules.extend([
        _rule("product.electrical", "product.mech", _COMPONENT_SUMMARY, "Mechanical placement needs component identity, not electrical pins."),
        _rule("product.electrical", "product.bom", _COMPONENT_SUMMARY, "BOM planning needs procurement identity, not pin-level data."),
        _rule("product.electrical", "product.firmware", {**_COMPONENT_SUMMARY, "pin_mappings": [], "buses": []}, "Firmware needs hardware interface assignments."),
        _rule("product.electrical", "product.assembly", {**_COMPONENT_SUMMARY, **_NET_SUMMARY}, "Assembly needs build-level connectivity without pin disclosure."),
        _rule("product.electrical", "product.validation", {"part_definitions": [], "components": [], "nets": [], "buses": [], "pin_mappings": [], "power_rails": []}, "Validation owns electrical safety checks and requires the complete electrical state."),
        _rule("product.mech", "product.bom", {"mechanical": [], "fabrication_notes": []}, "BOM planning may use mechanical sourcing and fabrication costs."),
        _rule("product.mech", "product.visuals", {"mechanical": [], "cad_model": [], "fabrication_notes": []}, "Visuals must render the canonical mechanical form rather than inventing it."),
        _rule("product.mech", "product.validation", {"mechanical": [], "fabrication_notes": []}, "Validation checks physical constraints and fabrication risks."),
        _rule("product.mech", "product.assembly", {"mechanical": [], "fabrication_notes": []}, "Assembly needs the canonical mechanical build context."),
        _rule("product.validation", "product.assembly", {"validation": []}, "Assembly must receive safety gates and blocking findings."),
        _rule("product.assembly", "project.docs", {"assembly": []}, "Documentation projects the approved builder-facing sequence."),
        _rule("product.bom", "project.docs", {"bom": []}, "Documentation may publish the approved procurement view."),
    ])
    return rules


DEFAULT_CONTEXT_GOVERNANCE_POLICY = ContextGovernancePolicy(rules=_default_rules())


__all__ = [
    "CONTEXT_GOVERNANCE_SCHEMA_VERSION",
    "ContextGovernancePolicy",
    "ContextProjection",
    "ContextShareMode",
    "ContextShareRule",
    "DEFAULT_CONTEXT_GOVERNANCE_POLICY",
    "project_context_for_agent",
]
