"""Bounded, reported native measurements; passing checks never certifies shape equivalence."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Finite = Annotated[float, Field(strict=True, ge=-1e12, le=1e12)]


class GeometryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    body_count: int = Field(strict=True, ge=1, le=10000)
    volume_mm3: float = Field(strict=True, gt=0, le=1e15)
    minimum_mm: tuple[Finite, Finite, Finite]
    maximum_mm: tuple[Finite, Finite, Finite]

    @model_validator(mode="after")
    def ordered(self):
        if any(lo >= hi for lo, hi in zip(self.minimum_mm, self.maximum_mm)):
            raise ValueError("Geometry bounds must have positive extent on every axis")
        return self


class NativeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    format: Literal["forma-native-evidence"] = "forma-native-evidence"
    version: Literal[1] = 1
    target: Literal["fusion360", "nx", "onshape"]
    application_version: str = Field(min_length=1, max_length=200)
    source_sha256: Digest
    rebuild_sha256: Digest
    status: Literal["rebuilt_unverified", "failed"]
    geometry: GeometryMetrics
    feature_ids: list[str] = Field(max_length=200)
    parameter_checks: dict[str, bool] = Field(max_length=200)
    metadata: dict = Field(max_length=10)
    native_artifacts: dict[str, Digest] = Field(default_factory=dict, max_length=4)
    source_geometry: GeometryMetrics | None = None
    source_step_sha256: Digest | None = None
    error: str | None = Field(default=None, max_length=4096)


def compare_evidence(model, target: str, evidence: NativeEvidence, *, source_step_sha256: str | None = None) -> dict:
    """Bind measurements to a package and compare in a common unit system.

    Tolerances are fixed here, not supplied in a potentially untrusted receipt.
    Reported volume/bounds do not establish topology or CAD-kernel equivalence.
    """
    import hashlib
    from .cli import _json
    expected = hashlib.sha256(_json(model.normalized())).hexdigest()
    if (evidence.source_sha256 != model.source.sha256 or evidence.rebuild_sha256 != expected
            or evidence.target != target):
        raise ValueError("Native evidence belongs to a different source, history or target")
    checks = {
        "execution_reported_success": evidence.status == "rebuilt_unverified" and not evidence.error,
        "feature_inventory": evidence.feature_ids == [f.id for f in model.features],
        "parameter_regeneration": bool(model.parameters) and set(evidence.parameter_checks) == set(model.parameters)
        and all(evidence.parameter_checks.values()),
        "metadata": evidence.metadata == model.metadata.model_dump(mode="json"),
    }
    source = model.source_geometry
    if source is None and evidence.source_geometry is not None:
        if not source_step_sha256 or evidence.source_step_sha256 != source_step_sha256:
            raise ValueError("Source measurements must be bound to the saved source STEP artifact")
        source = evidence.source_geometry
    if source is None:
        checks["source_measurements_available"] = False
    else:
        checks.update({
            "body_count": source.body_count == evidence.geometry.body_count == 1,
            "volume": abs(source.volume_mm3 - evidence.geometry.volume_mm3) <= max(0.01, source.volume_mm3 * 0.001),
            "bounds": all(abs(a - b) <= 0.01 for a, b in zip(
                (*source.minimum_mm, *source.maximum_mm),
                (*evidence.geometry.minimum_mm, *evidence.geometry.maximum_mm))),
        })
    differences = []
    expected_metadata = model.metadata.model_dump(mode="json")
    for key in sorted(set(expected_metadata) | set(evidence.metadata)):
        if expected_metadata.get(key) != evidence.metadata.get(key):
            differences.append({"property": key, "source": expected_metadata.get(key), "target": evidence.metadata.get(key)})
    return {"status": "checks_passed" if all(checks.values()) else "needs_repair", "checks": checks,
            "source_geometry": source.model_dump(mode="json") if source else None,
            "target_geometry": evidence.geometry.model_dump(mode="json"),
            "metadata_differences": differences,
            "parameter_checks": evidence.parameter_checks,
            "source_feature_ids": [f.id for f in model.features], "target_feature_ids": evidence.feature_ids,
            "evidence_origin": "user_uploaded_native_report", "geometry_equivalence_verified": False,
            "tolerances": {"bounds_mm": 0.01, "volume_relative": 0.001, "volume_absolute_mm3": 0.01},
            "limitations": ["Reported measurements are not authenticated native execution.",
                            "Matching volume and bounds do not prove equal surfaces, topology or design intent.",
                            "Engineering acceptance requires review of the native model and parameter behavior."]}
