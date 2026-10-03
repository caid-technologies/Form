"""Image persistence selected alongside the primary project database."""

import base64
import mimetypes
from forma_core.config import config
import re
import uuid
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlparse

from dotenv import load_dotenv

from forma_core.runtime import forma_dev_mode_enabled, primary_database_backend_from_environment

load_dotenv()

DEFAULT_BUCKET = "contents"
DEFAULT_SIGNED_URL_SECONDS = 60 * 60 * 24
SUPABASE_KEY_ENV_VARS = (
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SECRET_KEY",
)
IMAGE_STORAGE_BACKEND_ENV = "FORMA_IMAGE_STORAGE_BACKEND"


@dataclass(frozen=True)
class StoredImage:
    bucket: str
    key: str
    url: str
    s3_endpoint: str
    storage_method: str
    content_type: str
    size_bytes: int

    def metadata(self, prefix: str) -> Dict[str, Any]:
        return {
            f"{prefix}_url": self.url,
            f"{prefix}_s3_bucket": self.bucket,
            f"{prefix}_s3_key": self.key,
            f"{prefix}_s3_endpoint": self.s3_endpoint,
            f"{prefix}_storage_method": self.storage_method,
            f"{prefix}_content_type": self.content_type,
            f"{prefix}_size_bytes": self.size_bytes,
        }


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = config.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _first_env(names: Tuple[str, ...], default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = _env(name)
        if value is not None:
            return value
    return default


def _supabase_url() -> Optional[str]:
    return _env("SUPABASE_URL") or _env("NEXT_PUBLIC_SUPABASE_URL")


def _supabase_service_key() -> Optional[str]:
    return _first_env(SUPABASE_KEY_ENV_VARS)


def _supabase_project_ref() -> Optional[str]:
    supabase_url = _supabase_url()
    if not supabase_url:
        return None
    parsed = urlparse(supabase_url if "://" in supabase_url else f"https://{supabase_url}")
    host = parsed.netloc
    if host.endswith(".supabase.co"):
        return host.split(".", 1)[0]
    return None


def _derived_supabase_s3_endpoint() -> Optional[str]:
    """Derive the S3 endpoint from the configured Supabase project URL."""
    supabase_url = _supabase_url()
    if not supabase_url:
        return None

    project_ref = _supabase_project_ref()
    if project_ref:
        return f"https://{project_ref}.storage.supabase.co/storage/v1/s3"
    return f"{supabase_url.rstrip('/')}/storage/v1/s3"


def _public_base_url() -> Optional[str]:
    configured = _env("SUPABASE_STORAGE_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    supabase_url = _supabase_url()
    if supabase_url:
        return supabase_url.rstrip("/")
    endpoint = _env("SUPABASE_S3_ENDPOINT")
    if not endpoint:
        return None
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    if parsed.netloc.endswith(".storage.supabase.co"):
        project_ref = parsed.netloc.split(".", 1)[0]
        return f"https://{project_ref}.supabase.co"
    return None


def _image_storage_selection() -> Tuple[str, str]:
    if forma_dev_mode_enabled():
        return "local", "FORMA_DEV_MODE"

    configured = (_env(IMAGE_STORAGE_BACKEND_ENV) or "").lower()
    aliases = {
        "local": "local",
        "sqlite": "local",
        "inline": "local",
        "disabled": "local",
        "none": "local",
        "supabase": "supabase",
        "supabase-client": "supabase",
        "s3": "s3-compatible",
        "s3-compatible": "s3-compatible",
    }
    if configured:
        # An invalid explicit value fails closed instead of silently enabling a
        # remote ancillary store from credentials alone.
        return aliases.get(configured, "local"), IMAGE_STORAGE_BACKEND_ENV

    primary_backend = primary_database_backend_from_environment()
    if primary_backend == "supabase":
        return "supabase", "primary-database"
    return "local", "primary-database"


def _local_image_storage_config(selection_source: str, reason: str) -> Dict[str, Any]:
    return {
        "enabled": False,
        "provider": "database-inline",
        "write_method": None,
        "endpoint": None,
        "bucket": _env("SUPABASE_S3_BUCKET", DEFAULT_BUCKET),
        "region": _first_env(
            ("SUPABASE_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
            "us-east-1",
        ),
        "signed_url_seconds": _signed_url_seconds(),
        "public_base_url": None,
        "supabase_url_configured": bool(_supabase_url()),
        "service_key_configured": bool(_supabase_service_key()),
        "access_key_configured": bool(
            _first_env(("SUPABASE_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"))
        ),
        "secret_key_configured": bool(
            _first_env(("SUPABASE_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"))
        ),
        "dev_mode": forma_dev_mode_enabled(),
        "selection_source": selection_source,
        "disabled_reason": reason,
    }


def get_image_storage_config() -> Dict[str, Any]:
    storage_backend, selection_source = _image_storage_selection()
    if storage_backend == "local":
        if forma_dev_mode_enabled():
            reason = "FORMA_DEV_MODE stores image data inline with the selected project database."
        elif selection_source == IMAGE_STORAGE_BACKEND_ENV:
            reason = "Image storage is disabled by FORMA_IMAGE_STORAGE_BACKEND."
        else:
            reason = "Image storage follows the SQLite database; set FORMA_IMAGE_STORAGE_BACKEND=supabase to opt in."
        return _local_image_storage_config(selection_source, reason)

    supabase_url = _supabase_url()
    service_key = _supabase_service_key()
    access_key_id = _first_env(("SUPABASE_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"))
    secret_access_key = _first_env(("SUPABASE_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"))
    configured_s3_endpoint = _env("SUPABASE_S3_ENDPOINT")
    endpoint = configured_s3_endpoint or _derived_supabase_s3_endpoint()
    supabase_client_enabled = storage_backend == "supabase" and bool(supabase_url and service_key)
    s3_enabled = storage_backend == "s3-compatible" and bool(
        access_key_id and secret_access_key and configured_s3_endpoint
    )
    if storage_backend == "supabase" and not supabase_client_enabled and selection_source == "primary-database":
        # Preserve the existing S3-compatible fallback for Supabase-primary
        # deployments that provide only storage access credentials.
        s3_enabled = bool(access_key_id and secret_access_key and endpoint)
    config = {
        "enabled": supabase_client_enabled or s3_enabled,
        "provider": "supabase-storage",
        "write_method": "supabase-client" if supabase_client_enabled else "s3-compatible" if s3_enabled else None,
        "endpoint": endpoint,
        "bucket": _env("SUPABASE_S3_BUCKET", DEFAULT_BUCKET),
        "region": _first_env(("SUPABASE_S3_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"), "us-east-1"),
        "signed_url_seconds": _signed_url_seconds(),
        "public_base_url": _public_base_url(),
        "supabase_url_configured": bool(supabase_url),
        "service_key_configured": bool(service_key),
        "access_key_configured": bool(access_key_id),
        "secret_key_configured": bool(secret_access_key),
        "endpoint_configured": bool(configured_s3_endpoint),
        "dev_mode": False,
        "selection_source": selection_source,
    }
    if storage_backend == "s3-compatible" and not configured_s3_endpoint:
        config["disabled_reason"] = "S3-compatible image storage requires SUPABASE_S3_ENDPOINT."
    return config


def _signed_url_seconds() -> int:
    raw_value = _env("SUPABASE_IMAGE_SIGNED_URL_SECONDS")
    if raw_value:
        try:
            return max(60, int(raw_value))
        except ValueError:
            pass
    return DEFAULT_SIGNED_URL_SECONDS


def _supabase_storage_bucket(bucket: str):
    if get_image_storage_config().get("write_method") != "supabase-client":
        raise RuntimeError(
            "Supabase Storage is not selected. Set FORMA_IMAGE_STORAGE_BACKEND=supabase to opt in."
        )

    supabase_url = _supabase_url()
    service_key = _supabase_service_key()
    if not supabase_url or not service_key:
        raise RuntimeError("Supabase Storage read requires SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY.")

    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("Supabase client is not installed. Run pip install -r apps/api/requirements.txt.") from exc

    return create_client(supabase_url, service_key).storage.from_(bucket)


def _decode_image_payload(
    image_data: str,
    fallback_content_type: str = "image/png",
    *,
    allow_remote_url: bool = False,
) -> Tuple[bytes, str]:
    image_data = (image_data or "").strip()
    if not image_data:
        raise ValueError("Image data is empty.")

    if image_data.startswith(("http://", "https://")):
        if not allow_remote_url:
            raise ValueError("Remote image URLs are not accepted for this upload path.")
        request = urllib.request.Request(image_data, headers={"User-Agent": "Forma-OSS/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
            content_type = response.headers.get_content_type() or fallback_content_type
        return content, content_type

    content_type = fallback_content_type
    base64_data = image_data
    if "," in image_data:
        header, base64_data = image_data.split(",", 1)
        match = re.match(r"data:([^;]+);base64", header)
        if match:
            content_type = match.group(1)

    return base64.b64decode(base64_data.strip()), content_type


def _extension_for_content_type(content_type: str) -> str:
    normalized = (content_type or "image/png").split(";", 1)[0].strip().lower()
    if normalized == "image/jpeg":
        return "jpg"
    if normalized == "image/svg+xml":
        return "svg"
    guessed = mimetypes.guess_extension(normalized)
    if guessed:
        return guessed.lstrip(".")
    return "png"


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-._")
    return cleaned or "image"


def _project_uuid_path_part(project_id: Optional[str]) -> str:
    if not project_id:
        raise ValueError("Image upload requires a UUID project_id before writing to Supabase Storage.")
    try:
        return str(uuid.UUID(str(project_id).strip()))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Image upload requires a UUID project_id, got {project_id!r}.") from exc


def _object_key(prefix: str, project_id: Optional[str], content_type: str) -> str:
    extension = _extension_for_content_type(content_type)
    project_part = _project_uuid_path_part(project_id)
    prefix_part = _safe_path_part(prefix)
    return f"images/{project_part}/{prefix_part}-{uuid.uuid4().hex}.{extension}"


def _public_url(bucket: str, key: str, endpoint: str) -> str:
    public_base = _public_base_url()
    quoted_key = quote(key, safe="/")
    if public_base:
        return f"{public_base}/storage/v1/object/public/{bucket}/{quoted_key}"
    return f"{endpoint.rstrip('/')}/{bucket}/{quoted_key}"


def create_signed_image_url(bucket: str, key: str, expires_in: Optional[int] = None) -> Optional[str]:
    if not bucket or not key:
        return None
    if forma_dev_mode_enabled():
        return None
    bucket_proxy = _supabase_storage_bucket(bucket)
    signed = bucket_proxy.create_signed_url(key, expires_in or _signed_url_seconds())
    return signed.get("signedURL") or signed.get("signedUrl")


def _find_project_image_key(project_id: str, prefix: str, bucket: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    project_part = _project_uuid_path_part(project_id)
    folder = f"images/{project_part}"
    bucket_proxy = _supabase_storage_bucket(bucket)
    items = bucket_proxy.list(
        folder,
        {
            "limit": 100,
            "sortBy": {"column": "created_at", "order": "desc"},
        },
    )
    prefix_text = f"{_safe_path_part(prefix)}-"
    matches = [
        item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].startswith(prefix_text)
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
    item = matches[0]
    return f"{folder}/{item['name']}", item


def hydrate_image_storage_metadata(metadata: Dict[str, Any], project_id: Optional[str] = None, *, discover_missing: bool = True) -> Dict[str, Any]:
    metadata = dict(metadata or {})
    config = get_image_storage_config()
    if config.get("write_method") != "supabase-client":
        return metadata

    project_id = project_id or metadata.get("project_id")
    bucket = metadata.get("product_image_s3_bucket") or metadata.get("reference_image_s3_bucket") or config["bucket"]
    expires_in = config["signed_url_seconds"]

    image_prefixes = [
        ("product_image", "product"),
        ("product_case_image", "product-case"),
        ("product_inside_image", "product-inside"),
        ("product_diagram_image", "product-diagram"),
        ("reference_image", "reference"),
    ]

    for metadata_prefix, object_prefix in image_prefixes:
        key_name = f"{metadata_prefix}_s3_key"
        bucket_name = f"{metadata_prefix}_s3_bucket"
        url_name = f"{metadata_prefix}_url"

        image_bucket = metadata.get(bucket_name) or bucket
        image_key = metadata.get(key_name)
        if not image_key and project_id and discover_missing:
            try:
                found = _find_project_image_key(project_id, object_prefix, image_bucket)
            except Exception:
                found = None
            if found:
                image_key, item = found
                metadata[key_name] = image_key
                metadata[bucket_name] = image_bucket
                item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                if item_metadata.get("mimetype"):
                    metadata[f"{metadata_prefix}_content_type"] = item_metadata["mimetype"]
                if item_metadata.get("size"):
                    metadata[f"{metadata_prefix}_size_bytes"] = item_metadata["size"]

        if image_key:
            try:
                signed_url = create_signed_image_url(image_bucket, image_key, expires_in)
            except Exception as exc:
                metadata[f"{metadata_prefix}_storage_error"] = str(exc)[:500]
                continue
            if signed_url:
                existing_url = metadata.get(url_name)
                if existing_url and existing_url != signed_url and not str(existing_url).startswith("data:"):
                    metadata[f"{metadata_prefix}_public_url"] = existing_url
                metadata[url_name] = signed_url
                metadata[f"{metadata_prefix}_url_expires_in_seconds"] = expires_in
                metadata[f"{metadata_prefix}_storage_enabled"] = True
                metadata[f"{metadata_prefix}_storage_method"] = "supabase-client"

    sequence = metadata.get("product_visual_sequence")
    if isinstance(sequence, list):
        hydrated_sequence = []
        for item in sequence:
            if not isinstance(item, dict):
                hydrated_sequence.append(item)
                continue
            view_id = item.get("view_id")
            if not isinstance(view_id, str) or not view_id:
                hydrated_sequence.append(item)
                continue
            metadata_prefix = f"product_{view_id}_image"
            updated_item = dict(item)
            url = metadata.get(f"{metadata_prefix}_url")
            if url:
                updated_item["url"] = url
            if metadata.get(f"{metadata_prefix}_content_type"):
                updated_item["content_type"] = metadata[f"{metadata_prefix}_content_type"]
            if metadata.get(f"{metadata_prefix}_s3_bucket"):
                updated_item["s3_bucket"] = metadata[f"{metadata_prefix}_s3_bucket"]
            if metadata.get(f"{metadata_prefix}_s3_key"):
                updated_item["s3_key"] = metadata[f"{metadata_prefix}_s3_key"]
            hydrated_sequence.append(updated_item)
        metadata["product_visual_sequence"] = hydrated_sequence

    return metadata


def _upload_with_supabase_client(
    *,
    bucket: str,
    key: str,
    content: bytes,
    content_type: str,
    endpoint: str,
) -> StoredImage:
    supabase_url = _supabase_url()
    service_key = _supabase_service_key()
    if not supabase_url or not service_key:
        raise RuntimeError("Supabase Storage upload requires SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEY.")

    bucket_proxy = _supabase_storage_bucket(bucket)
    bucket_proxy.upload(
        key,
        content,
        file_options={
            "content-type": content_type,
            "cache-control": "31536000",
        },
    )
    return StoredImage(
        bucket=bucket,
        key=key,
        url=create_signed_image_url(bucket, key) or bucket_proxy.get_public_url(key),
        s3_endpoint=endpoint,
        storage_method="supabase-client",
        content_type=content_type,
        size_bytes=len(content),
    )


def _upload_with_s3_client(
    *,
    bucket: str,
    key: str,
    content: bytes,
    content_type: str,
    endpoint: str,
    region: str,
) -> StoredImage:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_first_env(("SUPABASE_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")),
        aws_secret_access_key=_first_env(("SUPABASE_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")),
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
        ContentType=content_type,
    )
    return StoredImage(
        bucket=bucket,
        key=key,
        url=_public_url(bucket, key, endpoint),
        s3_endpoint=endpoint,
        storage_method="s3-compatible",
        content_type=content_type,
        size_bytes=len(content),
    )


def upload_image_to_supabase_s3(
    image_data: str,
    *,
    prefix: str,
    project_id: Optional[str] = None,
    fallback_content_type: str = "image/png",
    allow_remote_url: bool = False,
) -> Optional[StoredImage]:
    config = get_image_storage_config()
    if not config["enabled"]:
        return None

    content, content_type = _decode_image_payload(
        image_data,
        fallback_content_type=fallback_content_type,
        allow_remote_url=allow_remote_url,
    )
    key = _object_key(prefix, project_id, content_type)
    if config["write_method"] == "supabase-client":
        return _upload_with_supabase_client(
            bucket=config["bucket"],
            key=key,
            content=content,
            content_type=content_type,
            endpoint=config["endpoint"],
        )
    return _upload_with_s3_client(
        bucket=config["bucket"],
        key=key,
        content=content,
        content_type=content_type,
        endpoint=config["endpoint"],
        region=config["region"],
    )


def delete_project_images(project_id: str) -> int:
    """Delete every remotely stored image beneath a project's isolated prefix."""
    config = get_image_storage_config()
    if not config.get("enabled"):
        return 0

    project_part = _project_uuid_path_part(project_id)
    folder = f"images/{project_part}"
    prefix = f"{folder}/"
    bucket = str(config["bucket"])

    if config.get("write_method") == "supabase-client":
        bucket_proxy = _supabase_storage_bucket(bucket)
        keys = []
        offset = 0
        while True:
            items = bucket_proxy.list(folder, {"limit": 1000, "offset": offset}) or []
            page_keys = [
                f"{folder}/{item['name']}"
                for item in items
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            keys.extend(key for key in page_keys if key.startswith(prefix))
            if len(items) < 1000:
                break
            offset += 1000
        for start in range(0, len(keys), 1000):
            bucket_proxy.remove(keys[start : start + 1000])
        remaining = bucket_proxy.list(folder, {"limit": 1}) or []
        if remaining:
            raise RuntimeError("Project image objects remain after deletion.")
        return len(keys)

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=config["endpoint"],
        aws_access_key_id=_first_env(("SUPABASE_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")),
        aws_secret_access_key=_first_env(("SUPABASE_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")),
        region_name=config["region"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    paginator = client.get_paginator("list_objects_v2")
    keys = [
        item["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents") or []
        if isinstance(item.get("Key"), str) and item["Key"].startswith(prefix)
    ]
    for start in range(0, len(keys), 1000):
        response = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]], "Quiet": True},
        )
        if response.get("Errors"):
            raise RuntimeError("One or more project image objects could not be deleted.")
    if (client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("Contents") or []):
        raise RuntimeError("Project image objects remain after deletion.")
    return len(keys)
