"""Concept images for the capability-bound OpenCode project."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from apps.api.a2a import _persist_mcp_compile
from apps.api.auth import UserContext
from apps.api.opencode_image_data import materialize_image
from forma_core.database import get_latest_project_revision, get_project_revision_by_source_job
from forma_core.image_providers import build_image_provider
from forma_core.opencode.capabilities import ConnectorCapability
from forma_core.opencode.models import GenerateImageArguments
from forma_core.persistence.images import upload_image_to_supabase_s3
from forma_core.workspaces.projects.state import ProjectStateError


def is_image_metadata(key: str) -> bool:
    return key.startswith(("product_image_", "product_case_image_", "product_inside_image_", "product_diagram_image_", "product_visual_sequence", "image_output_", "opencode_image_"))


def image_metadata_for_agent(metadata: dict[str, object]) -> dict[str, object]:
    """Keep model/status provenance, never send image bytes or storage URLs to the LLM."""
    return {key: value for key, value in metadata.items() if not is_image_metadata(key) or key in {
        "product_image_provider", "product_image_model", "product_image_size", "product_image_content_type",
        "image_output_status", "image_output_provider", "image_output_model", "opencode_image_source_revision",
    }}


class ImageToolError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(code)


def _result(revision: object, project_id: str) -> dict[str, object]:
    metadata = revision.state.assembly_metadata or {}
    return {
        "project_id": project_id,
        "revision_id": str(getattr(revision, "revision_id", None) or getattr(revision, "id", "")),
        "status": "succeeded",
        "provider": metadata.get("product_image_provider"),
        "model": metadata.get("product_image_model"),
        "source_revision_id": metadata.get("opencode_image_source_revision"),
        "message": "Concept image saved to the project preview. This image is not CAD or dimensional verification.",
    }


def generate_project_image(arguments: GenerateImageArguments, capability: ConnectorCapability,
                           *, before_save: Callable[[], None] | None = None) -> dict[str, object]:
    project_id, owner = capability.project_id, capability.owner_user_id
    # Identity, credentials and model are server-owned; the agent supplies only
    # a prompt and a retry key, never an owner, URL, path or API key.
    job_id = "opencode-image-" + hashlib.sha256(
        json.dumps([capability.session_id, project_id, arguments.request_id]).encode()
    ).hexdigest()[:32]
    prompt_digest = hashlib.sha256(arguments.prompt.encode()).hexdigest()
    previous = get_project_revision_by_source_job(project_id, owner, job_id)
    if previous is not None:
        if (previous.state.assembly_metadata or {}).get("opencode_image_prompt_digest") != prompt_digest:
            raise ImageToolError("image_request_conflict", "Use a new request_id for a different image prompt.")
        return _result(previous, project_id)

    try:
        source = get_latest_project_revision(project_id, owner)
    except ProjectStateError:
        raise ImageToolError("image_project_missing", "Create the bound project before generating an image.") from None
    if source is None:
        raise ImageToolError("image_project_missing", "Create the bound project before generating an image.")
    try:
        # Explicit image requests use the same server-owned provider settings as
        # Forma's other image flows, even when automatic image output is off.
        # An explicitly disabled IMAGE_PROVIDER remains disabled.
        provider = build_image_provider(force_enabled=True)
    except Exception:
        raise ImageToolError("image_provider_unavailable", "Image generation is not configured correctly on the Forma backend.") from None
    if not provider.is_configured:
        raise ImageToolError("image_provider_unavailable", "Image generation is not configured on the Forma backend. Check IMAGE_PROVIDER and its image credentials.")
    try:
        image = provider.generate_project_image(arguments.prompt, source.state)
    except Exception:
        raise ImageToolError("image_generation_failed", "Image generation failed. Check the backend image provider, model and API access before retrying.") from None
    if image is None:
        raise ImageToolError("image_generation_failed", "The image provider returned no image. No project changes were saved.")
    try:
        image_data, content_type = materialize_image(image.data_url)
    except Exception:
        raise ImageToolError("image_generation_failed", "The provider image could not be downloaded or contained unusable image data. No project changes were saved.") from None
    # Re-read before saving so a long provider call does not replace newer CAD
    # or other project edits with the input revision.
    latest = get_latest_project_revision(project_id, owner)
    if latest is None:
        raise ImageToolError("image_project_missing", "The project is no longer available.")
    project = latest.state.model_copy(deep=True)
    try:
        stored = upload_image_to_supabase_s3(
            image_data, project_id=project_id, prefix="product",
            fallback_content_type=content_type, allow_remote_url=False,
        )
    except Exception:
        raise ImageToolError("image_storage_failed", "The image could not be stored. No project changes were saved.") from None
    metadata = dict(project.assembly_metadata or {})
    # Replace the displayed visual set consistently; otherwise an old sequence
    # can take precedence over the newly generated product image in the UI.
    metadata = {key: value for key, value in metadata.items() if not is_image_metadata(key)}
    metadata.update({
        "product_image_provider": image.provider,
        "product_image_model": image.model,
        "product_image_size": image.size,
        "product_image_output_format": content_type.removeprefix("image/"),
        "product_image_content_type": content_type,
        "image_output_status": "succeeded",
        "image_output_provider": image.provider,
        "image_output_model": image.model,
        "image_output_failed": False,
        "image_output_error": None,
        "opencode_image_prompt_digest": prompt_digest,
        "opencode_image_source_revision": str(getattr(source, "revision_id", None) or getattr(source, "id", "")),
    })
    if stored is not None:
        metadata.update(stored.metadata("product_image"))
    else:
        # This is Forma's existing local/SQLite image persistence convention.
        metadata["product_image_data"] = image_data
    project.assembly_metadata = metadata
    user = UserContext(provider="opencode-connector", subject=owner, owner_user_id=owner, is_authenticated=True, is_admin=False)
    if before_save is not None:
        before_save()
    _persist_mcp_compile(project, {
        "project_id": project_id,
        "authoring_agent": "opencode", "source_job_id": job_id,
    }, user)
    saved = get_project_revision_by_source_job(project_id, owner, job_id)
    if saved is None:
        raise ImageToolError("image_persistence_failed", "The generated image could not be attached to a saved project revision.")
    return _result(saved, project_id)
