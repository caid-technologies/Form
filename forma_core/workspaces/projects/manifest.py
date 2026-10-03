"""Portable project manifests used by the local-first Forma CLI."""

from __future__ import annotations

from copy import deepcopy
import json
import mimetypes
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field


PROJECT_MANIFEST_FORMAT = "forma-project"
PROJECT_MANIFEST_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MEDIA_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_NON_PATH_KEYS = frozenset(
    {
        "filename",
        "name",
        "title",
        "description",
        "label",
        "format",
        "source",
        "url",
        "download_url",
        "source_url",
    }
)
_SECRET_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


def normalize_artifact_path(value: object) -> str:
    """Return a safe, portable project-relative artifact path."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("Artifact path must not be empty.")
    if (
        "\x00" in raw
        or "://" in raw
        or raw.startswith("/")
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw)
    ):
        raise ValueError(f"Artifact path must be project-relative: {value!r}")

    parts = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError(f"Artifact path may not escape the project directory: {value!r}")
        parts.append(part)
    if not parts:
        raise ValueError("Artifact path must identify a file.")
    return "/".join(parts)


def normalize_artifact_media_type(value: object) -> str:
    """Normalize and validate an artifact media type without parameters."""
    media_type = str(value or "").split(";", 1)[0].strip().lower()
    if not media_type or not _MEDIA_TYPE_PATTERN.fullmatch(media_type):
        raise ValueError(f"Invalid artifact media type: {value!r}")
    return media_type


def infer_artifact_media_type(path: str) -> str:
    """Infer a stable media type for common CAD files and ordinary documents."""
    suffix = Path(path).suffix.lower()
    if suffix in {".step", ".stp"}:
        return "model/step"
    if suffix == ".stl":
        return "model/stl"
    if suffix == ".3mf":
        return "model/3mf"
    if suffix in {".gcode", ".gc", ".gco"}:
        return "text/x-gcode"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def validate_artifact_references(
    artifacts: object,
    *,
    require_integrity: bool = True,
) -> list[dict[str, Any]]:
    """Normalize artifact declarations and reject unsafe or ambiguous entries."""
    if artifacts is None:
        return []
    if not isinstance(artifacts, list):
        raise ValueError("Project artifacts must be a list.")

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(artifacts):
        if isinstance(item, BaseModel):
            record = item.model_dump(mode="json")
        elif isinstance(item, Mapping):
            record = dict(item)
        else:
            raise ValueError(f"Project artifact {index + 1} must be an object.")
        path = normalize_artifact_path(record.get("path"))
        lower_path = path.lower()
        if lower_path == "forma-project.json" or lower_path == ".forma" or lower_path.startswith(".forma/"):
            raise ValueError(f"Project artifact path is reserved: {path}")
        path_key = path.casefold()
        if path_key in seen_paths:
            raise ValueError(f"Project artifact path is duplicated: {path}")
        seen_paths.add(path_key)
        record["path"] = path

        raw_sha256 = record.get("sha256")
        if raw_sha256 is None or not str(raw_sha256).strip():
            if require_integrity:
                raise ValueError(f"Project artifact {path} must declare a SHA-256 hash.")
        else:
            sha256 = str(raw_sha256).strip().lower()
            if not _SHA256_PATTERN.fullmatch(sha256):
                raise ValueError(f"Project artifact {path} has an invalid SHA-256 hash.")
            record["sha256"] = sha256

        raw_media_type = record.get("media_type")
        if raw_media_type is None or not str(raw_media_type).strip():
            if require_integrity:
                raise ValueError(f"Project artifact {path} must declare a media type.")
        else:
            record["media_type"] = normalize_artifact_media_type(raw_media_type)

        if "size_bytes" in record and record["size_bytes"] is not None:
            size = record["size_bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError(f"Project artifact {path} has an invalid size_bytes value.")
        normalized.append(record)
    return normalized


def rewrite_artifact_paths(value: Any, replacements: Mapping[str, str], *, _key: str | None = None) -> Any:
    """Rewrite known project-file references while preserving display filenames."""
    if isinstance(value, Mapping):
        return {
            str(key): rewrite_artifact_paths(item, replacements, _key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [rewrite_artifact_paths(item, replacements, _key=_key) for item in value]
    if isinstance(value, tuple):
        return [rewrite_artifact_paths(item, replacements, _key=_key) for item in value]
    if not isinstance(value, str) or (_key and _key.strip().lower() in _NON_PATH_KEYS):
        return deepcopy(value)

    candidates = (value, value.replace("\\", "/"))
    for candidate in candidates:
        replacement = replacements.get(candidate)
        if replacement is not None:
            return replacement
        suffix_matches: list[tuple[int, str]] = []
        for source, source_replacement in replacements.items():
            normalized_source = str(source).replace("\\", "/").strip("/")
            if normalized_source and candidate.rstrip("/").endswith(f"/{normalized_source}"):
                suffix_matches.append((len(normalized_source), source_replacement))
        if suffix_matches:
            return max(suffix_matches, key=lambda match: match[0])[1]
    return value


def _is_secret_field(name: object) -> bool:
    normalized = str(name).strip().lower().replace("-", "_")
    return normalized in {"key", "credential", "credentials"} or any(
        marker in normalized for marker in _SECRET_FIELD_MARKERS
    )


def redact_project_secrets(value: Any) -> Any:
    """Return a JSON-compatible copy without credential-like fields."""
    if isinstance(value, Mapping):
        return {
            str(key): redact_project_secrets(item)
            for key, item in value.items()
            if not _is_secret_field(key)
        }
    if isinstance(value, list):
        return [redact_project_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_project_secrets(item) for item in value]
    return deepcopy(value)


class ProjectArtifactReference(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    sha256: str | None = None
    media_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class ProjectManifest(BaseModel):
    """Canonical, uploadable representation of one local Forma project."""

    model_config = ConfigDict(extra="allow")

    format: str = PROJECT_MANIFEST_FORMAT
    version: int = Field(default=PROJECT_MANIFEST_VERSION, ge=1)
    project_id: str = Field(min_length=1)
    workspace_id: str | None = None
    title: str = ""
    prompt: str = ""
    visibility: str = "public"
    project_ir: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ProjectArtifactReference] = Field(default_factory=list)

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "ProjectManifest":
        """Accept both the canonical wrapper and legacy raw Hardware Intermediate Representation JSON."""
        raw = dict(document)
        nested = raw.get("project_ir")
        if not isinstance(nested, Mapping):
            nested = raw.get("hardware_ir")
        project_ir = dict(nested) if isinstance(nested, Mapping) else raw
        metadata = project_ir.get("assembly_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        project_id = raw.get("project_id") or metadata.get("project_id") or str(uuid4())
        overview = project_ir.get("overview")
        overview = overview if isinstance(overview, Mapping) else {}
        title = raw.get("title") or overview.get("title") or "Untitled Forma Project"
        prompt = raw.get("prompt") or metadata.get("project_prompt") or ""
        artifact_values = []
        for item in raw.get("artifacts") or []:
            if not isinstance(item, Mapping):
                continue
            artifact = dict(item)
            artifact.setdefault("path", artifact.get("uri") or artifact.get("name") or "")
            artifact_values.append(artifact)
        return cls(
            format=str(raw.get("format") or PROJECT_MANIFEST_FORMAT),
            version=int(raw.get("version") or PROJECT_MANIFEST_VERSION),
            project_id=str(project_id),
            workspace_id=str(raw["workspace_id"]) if raw.get("workspace_id") else None,
            title=str(title),
            prompt=str(prompt),
            visibility=str(raw.get("visibility") or "public").strip().lower(),
            project_ir=project_ir,
            artifacts=[ProjectArtifactReference.model_validate(item) for item in artifact_values],
        )

    def upload_payload(self) -> dict[str, Any]:
        """Build the only payload shape that may cross the cloud boundary."""
        return redact_project_secrets(self.model_dump(mode="json"))


def build_canonical_revision_record(
    project_record: Mapping[str, Any],
    revision_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build a unified revision when a CLI identifier and IR are canonicalizable."""
    try:
        project_id = UUID(str(project_record["project_id"]).strip())
        revision_id = UUID(str(revision_record["revision_id"]).strip())
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

    manifest = revision_record.get("manifest_json")
    if not isinstance(manifest, Mapping):
        return None
    try:
        from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
        from forma_core.workspaces.projects.state import ProjectRevision

        state = HardwareIntermediateRepresentation.model_validate(manifest.get("project_ir") or {})
        artifacts = []
        for item in validate_artifact_references(manifest.get("artifacts"), require_integrity=False):
            path = str(item["path"])
            artifact = {
                "artifact_id": str(item.get("sha256") or path),
                "kind": "file",
                "uri": path,
                "media_type": item.get("media_type"),
                "checksum": item.get("sha256"),
                "metadata": {"size_bytes": item.get("size_bytes")} if item.get("size_bytes") is not None else {},
            }
            artifacts.append(artifact)
        payload = {
            "schema_version": "1.0",
            "revision_id": str(revision_id),
            "project_id": str(project_id),
            "owner_user_id": str(project_record["owner_user_id"]),
            "revision": int(revision_record["revision"]),
            "parent_revision": max(1, int(revision_record["revision"]) - 1)
            if int(revision_record["revision"]) > 1
            else None,
            "design_brief_id": str(uuid5(NAMESPACE_URL, f"forma-cli-brief:{project_id}")),
            "design_brief_version": 1,
            "source_job_id": f"cli:{revision_id}",
            "created_at": revision_record["created_at"],
            "state": state.model_dump(mode="json"),
            "components": [item.model_dump(mode="json") for item in state.components],
            "systems": [],
            "artifacts": artifacts,
            "assumptions": [],
        }
        ProjectRevision.model_validate(payload)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "id": str(revision_id),
        "project_id": str(project_id),
        "owner_user_id": str(project_record["owner_user_id"]),
        "revision": int(revision_record["revision"]),
        "parent_revision": payload["parent_revision"],
        "design_brief_id": payload["design_brief_id"],
        "design_brief_version": 1,
        "source_job_id": payload["source_job_id"],
        "payload_json": payload,
        "created_at": revision_record["created_at"],
    }


def load_project_manifest(path: str | Path) -> ProjectManifest:
    project_path = Path(path)
    with project_path.open(encoding="utf-8") as file:
        document = json.load(file)
    if not isinstance(document, Mapping):
        raise ValueError(f"Project manifest must contain a JSON object: {project_path}")
    return ProjectManifest.from_document(document)


def write_project_manifest(path: str | Path, manifest: ProjectManifest) -> None:
    project_path = Path(path)
    safe_payload = redact_project_secrets(manifest.model_dump(mode="json"))
    project_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = project_path.with_suffix(f"{project_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(safe_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(project_path)


__all__ = [
    "PROJECT_MANIFEST_FORMAT",
    "PROJECT_MANIFEST_VERSION",
    "ProjectArtifactReference",
    "ProjectManifest",
    "build_canonical_revision_record",
    "infer_artifact_media_type",
    "load_project_manifest",
    "normalize_artifact_media_type",
    "normalize_artifact_path",
    "redact_project_secrets",
    "rewrite_artifact_paths",
    "validate_artifact_references",
    "write_project_manifest",
]
