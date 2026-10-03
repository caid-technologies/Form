"""Private storage for artifacts transferred by the Forma OSS CLI."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forma_core.config import config
from forma_core.config.runtime import forma_dev_mode_enabled, primary_database_backend_from_environment
from forma_core.workspaces.projects.manifest import normalize_artifact_media_type


DEFAULT_PROJECT_ARTIFACT_BUCKET = "cli-project-artifacts"
DEFAULT_PROJECT_ARTIFACT_DIRECTORY = ".forma/cli-artifacts"
DEFAULT_PROJECT_ARTIFACT_MAX_BYTES = 50 * 1024 * 1024


class ProjectArtifactStorageError(RuntimeError):
    """Raised when private project artifact storage cannot be used."""


@dataclass(frozen=True)
class StoredProjectArtifact:
    project_id: str
    sha256: str
    media_type: str
    size_bytes: int
    content: bytes | None = None


def _storage_backend() -> str:
    configured = (config.optional("FORMA_CLI_ARTIFACT_STORAGE_BACKEND") or "").lower()
    aliases = {
        "local": "local",
        "sqlite": "local",
        "supabase": "supabase",
        "supabase-client": "supabase",
        "s3": "s3-compatible",
        "s3-compatible": "s3-compatible",
    }
    if configured:
        return aliases.get(configured, "local")
    if forma_dev_mode_enabled():
        return "local"
    return "supabase" if primary_database_backend_from_environment() == "supabase" else "local"


def get_project_artifact_storage_config() -> dict[str, Any]:
    """Resolve artifact storage without constructing a remote client."""
    backend = _storage_backend()
    bucket = config.optional("FORMA_CLI_ARTIFACT_BUCKET") or DEFAULT_PROJECT_ARTIFACT_BUCKET
    directory = Path(
        config.optional("FORMA_CLI_ARTIFACT_STORAGE_DIR") or DEFAULT_PROJECT_ARTIFACT_DIRECTORY
    ).expanduser()
    try:
        max_bytes = max(1, int(config.optional("FORMA_CLI_ARTIFACT_MAX_BYTES") or DEFAULT_PROJECT_ARTIFACT_MAX_BYTES))
    except ValueError:
        max_bytes = DEFAULT_PROJECT_ARTIFACT_MAX_BYTES
    supabase_url = config.optional("SUPABASE_URL") or config.optional("NEXT_PUBLIC_SUPABASE_URL")
    service_key = config.optional("SUPABASE_SERVICE_ROLE_KEY") or config.optional("SUPABASE_SECRET_KEY")
    endpoint = config.optional("SUPABASE_S3_ENDPOINT")
    access_key = config.optional("SUPABASE_S3_ACCESS_KEY_ID") or config.optional("AWS_ACCESS_KEY_ID")
    secret_key = config.optional("SUPABASE_S3_SECRET_ACCESS_KEY") or config.optional("AWS_SECRET_ACCESS_KEY")
    enabled = (
        backend == "local"
        or backend == "supabase" and bool(supabase_url and service_key)
        or backend == "s3-compatible" and bool(endpoint and access_key and secret_key)
    )
    return {
        "enabled": enabled,
        "backend": backend,
        "bucket": bucket,
        "directory": directory,
        "max_bytes": max_bytes,
        "supabase_url_configured": bool(supabase_url),
        "service_key_configured": bool(service_key),
        "endpoint_configured": bool(endpoint),
        "access_key_configured": bool(access_key),
        "secret_key_configured": bool(secret_key),
    }


def project_artifact_storage_key(project_id: str, sha256: str) -> str:
    """Build a server-owned, project-scoped object key."""
    project_text = str(project_id or "").strip()
    if not project_text:
        raise ValueError("Project artifact storage requires a project_id.")
    digest = str(sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Project artifact storage requires a SHA-256 hash.")
    project_scope = hashlib.sha256(project_text.encode("utf-8")).hexdigest()
    return f"projects/{project_scope}/artifacts/{digest}"


class ProjectArtifactStorage:
    """Read and write private objects using Supabase, S3, or local disk."""

    def __init__(self, storage_config: dict[str, Any] | None = None) -> None:
        self.config = storage_config if storage_config is not None else get_project_artifact_storage_config()

    def _require_enabled(self) -> None:
        if self.config.get("enabled"):
            return
        backend = self.config.get("backend")
        if backend == "supabase":
            raise ProjectArtifactStorageError(
                "CLI artifact storage requires SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY."
            )
        if backend == "s3-compatible":
            raise ProjectArtifactStorageError(
                "CLI artifact S3 storage requires SUPABASE_S3_ENDPOINT and access credentials."
            )
        raise ProjectArtifactStorageError("CLI artifact storage is not configured.")

    def _validate_size(self, content: bytes) -> None:
        if len(content) > int(self.config["max_bytes"]):
            raise ProjectArtifactStorageError(
                f"Project artifact exceeds the {self.config['max_bytes']} byte size limit."
            )

    def _supabase_bucket(self):
        supabase_url = config.optional("SUPABASE_URL") or config.optional("NEXT_PUBLIC_SUPABASE_URL")
        service_key = config.optional("SUPABASE_SERVICE_ROLE_KEY") or config.optional("SUPABASE_SECRET_KEY")
        if not supabase_url or not service_key:
            raise ProjectArtifactStorageError(
                "CLI artifact storage requires SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY."
            )
        try:
            from supabase import create_client
        except ImportError as exc:
            raise ProjectArtifactStorageError(
                "Supabase client is not installed. Run pip install -r apps/api/requirements.txt."
            ) from exc
        return create_client(supabase_url, service_key).storage.from_(self.config["bucket"])

    def _s3_client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise ProjectArtifactStorageError("boto3 is required for S3-compatible artifact storage.") from exc
        endpoint = config.optional("SUPABASE_S3_ENDPOINT")
        access_key = config.optional("SUPABASE_S3_ACCESS_KEY_ID") or config.optional("AWS_ACCESS_KEY_ID")
        secret_key = config.optional("SUPABASE_S3_SECRET_ACCESS_KEY") or config.optional("AWS_SECRET_ACCESS_KEY")
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=config.optional("SUPABASE_S3_REGION") or config.optional("AWS_REGION") or "us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def put(self, project_id: str, sha256: str, content: bytes, media_type: str) -> StoredProjectArtifact:
        self._require_enabled()
        self._validate_size(content)
        normalized_sha256 = str(sha256 or "").strip().lower()
        key = project_artifact_storage_key(project_id, normalized_sha256)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != normalized_sha256:
            raise ProjectArtifactStorageError(
                f"Project artifact content does not match its declared SHA-256 hash: {normalized_sha256}."
            )
        try:
            normalized_media_type = normalize_artifact_media_type(media_type)
        except ValueError as exc:
            raise ProjectArtifactStorageError("Project artifact media type is invalid.") from exc
        backend = self.config["backend"]
        if backend == "local":
            target = Path(self.config["directory"]) / Path(*key.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f"{target.suffix}.tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        elif backend == "supabase":
            bucket = self._supabase_bucket()
            options = {
                "content-type": normalized_media_type,
                "cache-control": "31536000",
                "upsert": "true",
            }
            try:
                bucket.upload(key, content, file_options=options)
            except Exception:
                # A retry may target an object that the first request created.
                try:
                    bucket.update(key, content, file_options=options)
                except Exception as update_exc:
                    raise ProjectArtifactStorageError("Project artifact storage upload failed.") from update_exc
        else:
            try:
                self._s3_client().put_object(
                    Bucket=self.config["bucket"],
                    Key=key,
                    Body=content,
                    ContentType=normalized_media_type,
                )
            except Exception as exc:
                raise ProjectArtifactStorageError("Project artifact storage upload failed.") from exc
        return StoredProjectArtifact(project_id, normalized_sha256, normalized_media_type, len(content))

    def get(self, project_id: str, sha256: str, media_type: str) -> StoredProjectArtifact:
        self._require_enabled()
        normalized_sha256 = str(sha256 or "").strip().lower()
        key = project_artifact_storage_key(project_id, normalized_sha256)
        try:
            normalized_media_type = normalize_artifact_media_type(media_type)
        except ValueError as exc:
            raise ProjectArtifactStorageError("Project artifact media type is invalid.") from exc
        backend = self.config["backend"]
        if backend == "local":
            content = (Path(self.config["directory"]) / Path(*key.split("/"))).read_bytes()
        elif backend == "supabase":
            try:
                content = bytes(self._supabase_bucket().download(key))
            except FileNotFoundError:
                raise
            except Exception as exc:
                raise ProjectArtifactStorageError("Project artifact storage download failed.") from exc
        else:
            try:
                content = self._s3_client().get_object(Bucket=self.config["bucket"], Key=key)["Body"].read()
            except Exception as exc:
                raise ProjectArtifactStorageError("Project artifact storage download failed.") from exc
        self._validate_size(content)
        return StoredProjectArtifact(project_id, normalized_sha256, normalized_media_type, len(content), content)

    def contains(self, project_id: str, sha256: str) -> bool:
        """Return whether an object with the declared SHA-256 is already stored."""
        self._require_enabled()
        normalized_sha256 = str(sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_sha256):
            raise ValueError("Project artifact storage requires a SHA-256 hash.")
        key = project_artifact_storage_key(project_id, normalized_sha256)
        backend = self.config["backend"]
        if backend == "local":
            return (Path(self.config["directory"]) / Path(*key.split("/"))).is_file()
        if backend == "supabase":
            bucket = self._supabase_bucket()
            folder, _, name = key.rpartition("/")
            try:
                # Supabase storage treats a list prefix as a folder, so listing
                # the full object key would only return its children. List the
                # containing folder and match the object name instead.
                items = bucket.list(folder, {"limit": 1000, "search": name}) or []
            except Exception as exc:
                raise ProjectArtifactStorageError("Project artifact presence check failed.") from exc
            return any(
                isinstance(item, dict) and item.get("name") == name
                for item in items
            )
        try:
            self._s3_client().head_object(Bucket=self.config["bucket"], Key=key)
            return True
        except Exception:
            return False

    def delete_project(self, project_id: str) -> int:
        """Delete all objects for a project; used by privacy purge workers."""
        self._require_enabled()
        project_text = str(project_id or "").strip()
        if not project_text:
            raise ValueError("Project artifact deletion requires a project_id.")
        scope = hashlib.sha256(project_text.encode("utf-8")).hexdigest()
        prefix = f"projects/{scope}/artifacts/"
        backend = self.config["backend"]
        if backend == "local":
            root = Path(self.config["directory"]) / Path(*prefix.split("/")[:-1])
            if not root.exists():
                return 0
            count = sum(1 for item in root.iterdir() if item.is_file())
            for item in root.iterdir():
                if item.is_file():
                    item.unlink()
            return count
        if backend == "supabase":
            bucket = self._supabase_bucket()
            items: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = bucket.list(prefix.removesuffix("/"), {"limit": 1000, "offset": offset}) or []
                items.extend(item for item in page if isinstance(item, dict))
                if len(page) < 1000:
                    break
                offset += 1000
            keys = [f"{prefix}{item['name']}" for item in items if item.get("name")]
            for start in range(0, len(keys), 1000):
                bucket.remove(keys[start : start + 1000])
            return len(keys)
        client = self._s3_client()
        paginator = client.get_paginator("list_objects_v2")
        keys = [
            item["Key"]
            for page in paginator.paginate(Bucket=self.config["bucket"], Prefix=prefix)
            for item in page.get("Contents") or []
            if item.get("Key")
        ]
        for start in range(0, len(keys), 1000):
            client.delete_objects(
                Bucket=self.config["bucket"],
                Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]], "Quiet": True},
            )
        return len(keys)


__all__ = [
    "DEFAULT_PROJECT_ARTIFACT_BUCKET",
    "ProjectArtifactStorage",
    "ProjectArtifactStorageError",
    "StoredProjectArtifact",
    "get_project_artifact_storage_config",
    "project_artifact_storage_key",
]
