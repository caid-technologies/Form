"""Local artifact preparation and integrity checks for cloud project sync."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from forma_cli.local import LocalProjectError
from forma_core.workspaces.projects.manifest import (
    ProjectManifest,
    infer_artifact_media_type,
    normalize_artifact_media_type,
    normalize_artifact_path,
    redact_project_secrets,
    rewrite_artifact_paths,
)


@dataclass(frozen=True)
class LocalProjectArtifact:
    """A validated local file ready to be transferred."""

    path: str
    source_path: Path
    sha256: str
    media_type: str
    size_bytes: int


def _portable_path(root: Path, value: object, *, require_file: bool = True) -> tuple[str, Path]:
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser()
    windows_absolute = PureWindowsPath(raw).is_absolute() or bool(PureWindowsPath(raw).drive)
    if candidate.is_absolute() or windows_absolute:
        source = candidate.resolve()
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise LocalProjectError(
                f"Project artifact must be inside the project directory: {value!r}"
            ) from exc
        portable = relative.as_posix()
    else:
        portable = normalize_artifact_path(raw)
        source = (root / Path(*portable.split("/"))).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise LocalProjectError(
                f"Project artifact must be inside the project directory: {value!r}"
            ) from exc
    if require_file and not source.is_file():
        raise LocalProjectError(f"Project artifact does not exist: {source}")
    return portable, source


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def canonical_project_upload_payload(root: str | Path, manifest: ProjectManifest) -> dict[str, Any]:
    """Canonicalize local file paths without requiring artifact bytes to exist."""
    project_root = Path(root).expanduser().resolve()
    payload = manifest.upload_payload()
    references: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    for artifact in manifest.artifacts:
        portable, source = _portable_path(project_root, artifact.path, require_file=False)
        lower_portable = portable.lower()
        if lower_portable == "forma-project.json" or lower_portable == ".forma" or lower_portable.startswith(".forma/"):
            raise LocalProjectError(f"Project artifact path is reserved: {portable}")
        reference = redact_project_secrets(artifact.model_dump(mode="json"))
        reference["path"] = portable
        if reference.get("sha256"):
            reference["sha256"] = str(reference["sha256"]).strip().lower()
        if reference.get("media_type"):
            reference["media_type"] = normalize_artifact_media_type(reference["media_type"])
        references.append(reference)
        replacements[str(artifact.path)] = portable
        replacements[str(artifact.path).replace("\\", "/")] = portable
        replacements[str(source)] = portable
        replacements[source.as_posix()] = portable
    payload["artifacts"] = references
    payload["project_ir"] = rewrite_artifact_paths(payload.get("project_ir", {}), replacements)
    return payload


def prepare_project_upload(
    root: str | Path,
    manifest: ProjectManifest,
) -> tuple[dict[str, Any], list[LocalProjectArtifact]]:
    """Validate referenced files and return a portable upload manifest."""
    project_root = Path(root).expanduser().resolve()
    payload = canonical_project_upload_payload(project_root, manifest)
    references: list[dict[str, Any]] = []
    prepared: list[LocalProjectArtifact] = []
    seen_paths: set[str] = set()
    replacements: dict[str, str] = {}

    for artifact in manifest.artifacts:
        portable, source = _portable_path(project_root, artifact.path)
        lower_portable = portable.lower()
        if lower_portable == "forma-project.json" or lower_portable == ".forma" or lower_portable.startswith(".forma/"):
            raise LocalProjectError(f"Project artifact path is reserved: {portable}")
        portable_key = portable.casefold()
        if portable_key in seen_paths:
            raise LocalProjectError(f"Project artifact path is duplicated: {portable}")
        seen_paths.add(portable_key)
        sha256, size_bytes = _file_digest(source)
        declared_sha256 = (artifact.sha256 or "").strip().lower()
        if declared_sha256 and declared_sha256 != sha256:
            raise LocalProjectError(
                f"Project artifact hash mismatch for {portable}: "
                f"manifest declares {declared_sha256}, local file is {sha256}."
            )
        try:
            media_type = normalize_artifact_media_type(artifact.media_type) if artifact.media_type else None
        except ValueError as exc:
            raise LocalProjectError(f"Project artifact {portable} has an invalid media type.") from exc
        media_type = media_type or infer_artifact_media_type(portable)
        reference = redact_project_secrets(artifact.model_dump(mode="json"))
        reference.update(
            {
                "path": portable,
                "sha256": sha256,
                "media_type": media_type,
                "size_bytes": size_bytes,
            }
        )
        references.append(reference)
        prepared.append(
            LocalProjectArtifact(
                path=portable,
                source_path=source,
                sha256=sha256,
                media_type=media_type,
                size_bytes=size_bytes,
            )
        )
        replacements[str(artifact.path)] = portable
        replacements[str(artifact.path).replace("\\", "/")] = portable
        replacements[str(source)] = portable
        replacements[source.as_posix()] = portable

    payload["artifacts"] = references
    payload["project_ir"] = rewrite_artifact_paths(payload.get("project_ir", {}), replacements)
    return payload, prepared


def file_digest(path: str | Path) -> str:
    """Return a file SHA-256 for working-tree conflict checks."""
    digest, _size = _file_digest(Path(path))
    return digest


def artifact_target(root: str | Path, path: str) -> Path:
    """Resolve a remote artifact path beneath the local project root."""
    project_root = Path(root).expanduser().resolve()
    portable = normalize_artifact_path(path)
    target = project_root / Path(*portable.split("/"))
    try:
        target.resolve().relative_to(project_root)
    except ValueError as exc:
        raise LocalProjectError(f"Project artifact target escapes the project directory: {path!r}") from exc
    return target


__all__ = [
    "LocalProjectArtifact",
    "artifact_target",
    "canonical_project_upload_payload",
    "file_digest",
    "prepare_project_upload",
]
