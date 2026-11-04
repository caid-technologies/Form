"""Portable, deterministic project bundles containing inspectable files only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile
from pydantic import Field, field_validator
from typing import Literal

from .inspection import ArtifactReader, inspect_project
from .models import HardwareIntermediateRepresentation
from .manifest import (ProjectArtifactReference, ProjectManifest, normalize_artifact_path, normalize_artifact_media_type, redact_project_secrets,
                       validate_artifact_references, write_project_manifest)

PROJECT_FILENAME = "forma-project.json"
INSPECTION_FILENAME = "forma-inspection.json"
MAX_BUNDLE_BYTES = 250 * 1024 * 1024
MAX_BUNDLE_FILES = 1000


class PortableArtifactReference(ProjectArtifactReference):
    path: str = Field(description="Canonical slash-separated project-relative file path; no dot segments, reserved report names, or .forma files.",
                      pattern=r"^[^/\\:\u0000]+(?:/[^/\\:\u0000]+)*$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=3)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        path = normalize_artifact_path(value)
        if path != value:
            raise ValueError("Portable artifact paths must use canonical relative slash-separated names.")
        if path.casefold() in {PROJECT_FILENAME, INSPECTION_FILENAME, ".forma"} or path.casefold().startswith(".forma/"):
            raise ValueError("Project artifact path is reserved.")
        return path

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value):
        return normalize_artifact_media_type(value)


class PortableHardwareIR(HardwareIntermediateRepresentation):
    hardware_ir_version: Literal["0.2"] = "0.2"


class PortableProjectManifest(ProjectManifest):
    """Strict integrity contract for portable delivery; legacy imports stay separate."""
    project_ir: PortableHardwareIR
    artifacts: list[PortableArtifactReference] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("project_ir", mode="before")
    @classmethod
    def supported_ir_version(cls, value):
        from forma_core.config.compatibility import ensure_supported_hardware_ir_version
        if isinstance(value, dict):
            ensure_supported_hardware_ir_version(value)
        return value


def local_artifact_reader(root: str | Path) -> ArtifactReader:
    """An explicit local adapter; hosted interfaces must use authorized storage."""
    directory = Path(root).expanduser().resolve()
    def read(reference):
        relative = normalize_artifact_path(reference.path)
        source = (directory / relative).resolve()
        if not source.is_relative_to(directory):
            raise ValueError("Project artifact escapes its directory.")
        return source.read_bytes()
    return read


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def export_project_bundle(manifest: ProjectManifest, destination: str | Path, *, read_artifact: ArtifactReader) -> Path:
    """Write an inspectable ZIP; failed engineering checks remain in its report.

    Every declared file must be available and match its declared checksum. Export
    never runs a source file and never converts a validation result to approval.
    """
    PortableProjectManifest.model_validate(manifest.model_dump(mode="json"))
    references = validate_artifact_references([item.model_dump(mode="json") for item in manifest.artifacts])
    if len(references) + 2 > MAX_BUNDLE_FILES:
        raise ValueError("Project bundle has too many files.")
    contents: dict[str, bytes] = {}
    total = 0
    for reference in manifest.artifacts:
        path = normalize_artifact_path(reference.path)
        if path.casefold() == INSPECTION_FILENAME:
            raise ValueError("Artifact path is reserved for the bundle inspection report.")
        content = read_artifact(reference)
        if not isinstance(content, bytes):
            raise TypeError("Artifact resolver must return bytes.")
        total += len(content)
        if total > MAX_BUNDLE_BYTES:
            raise ValueError("Project bundle exceeds the byte limit.")
        contents[path] = content
    report = inspect_project(manifest, read_artifact=lambda reference: contents[normalize_artifact_path(reference.path)])
    if any(check.status != "pass" for check in report.checks if check.name.startswith("artifact")):
        raise ValueError("Project bundle requires verified artifact declarations and bytes.")
    if any(check.status == "fail" and check.name in {"hardware_ir", "manifest_version"} for check in report.checks):
        raise ValueError("Project bundle requires a supported manifest and schema-valid HardwareIR.")
    contents[PROJECT_FILENAME] = _json_bytes(redact_project_secrets(manifest.model_dump(mode="json")))
    contents[INSPECTION_FILENAME] = _json_bytes(report.model_dump(mode="json"))
    if sum(map(len, contents.values())) > MAX_BUNDLE_BYTES:
        raise ValueError("Project bundle exceeds the byte limit.")
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with zipfile.ZipFile(temporary_path, mode="w", compression=zipfile.ZIP_STORED) as archive:
            for path, content in sorted(contents.items()):
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, content)
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def import_project_bundle(source: str | Path, destination: str | Path) -> ProjectManifest:
    """Verify an entire bundle before atomically creating a new project directory."""
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise ValueError("Bundle import requires a new destination directory.")
    with zipfile.ZipFile(source) as archive:
        files = archive.infolist()
        if len(files) > MAX_BUNDLE_FILES or sum(info.file_size for info in files) > MAX_BUNDLE_BYTES:
            raise ValueError("Project bundle exceeds supported limits.")
        names: set[str] = set()
        for info in files:
            path = normalize_artifact_path(info.filename)
            if path != info.filename or path.casefold() in names:
                raise ValueError("Bundle contains noncanonical or duplicate paths.")
            names.add(path.casefold())
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Bundle entries must be regular files.")
        if PROJECT_FILENAME not in {info.filename for info in files}:
            raise ValueError("Bundle has no project manifest.")
        document = json.loads(archive.read(PROJECT_FILENAME))
        # Delivery is strict. Legacy import adapters must not erase malformed
        # declarations or normalize an unknown schema before this validation.
        PortableProjectManifest.model_validate(document)
        manifest = ProjectManifest.from_document(document)
        references = validate_artifact_references([item.model_dump(mode="json") for item in manifest.artifacts])
        expected = {PROJECT_FILENAME, INSPECTION_FILENAME, *(reference["path"] for reference in references)}
        if {info.filename for info in files} != expected:
            raise ValueError("Bundle files must exactly match the manifest and inspection report.")
        report = inspect_project(manifest, read_artifact=lambda reference: archive.read(reference.path))
        if any(check.status != "pass" for check in report.checks if check.name.startswith("artifact")):
            raise ValueError("Bundle artifact verification failed.")
        if any(check.name == "hardware_ir" and check.status == "fail" for check in report.checks):
            raise ValueError("Bundle HardwareIR is invalid.")
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}."))
        try:
            for info in files:
                output = staging / info.filename
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(info))
            # Stored inspection is historical. Replace it with freshly computed
            # facts; imported payloads cannot forge current validator results.
            (staging / INSPECTION_FILENAME).write_bytes(_json_bytes(report.model_dump(mode="json")))
            write_project_manifest(staging / PROJECT_FILENAME, manifest)
            if target.exists():
                raise ValueError("Bundle destination was created concurrently.")
            os.rename(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return manifest


__all__ = ["PortableProjectManifest", "export_project_bundle", "import_project_bundle", "local_artifact_reader"]
