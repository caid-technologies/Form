"""Post-generation project output: hierarchical visuals and canonical persistence."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from forma_core.database import (
    claim_unowned_generated_project,
    persist_chat_project_revision,
    persist_legacy_project_projection,
    init_db,
    refresh_legacy_project_projection,
)
from forma_core.images import build_image_provider, build_project_visual_spec
from forma_core.persistence.images import get_image_storage_config, upload_image_to_supabase_s3
from forma_core.user_integrations import ResolvedIntegrationSettings
from forma_core.workspaces.projects.design_lifecycle import (
    RepresentationKind,
    RepresentationStatus,
    VisualApprovalPolicy,
    VisualApprovalStatus,
    bootstrap_design_lifecycle,
    load_design_lifecycle,
    register_system_render,
    register_system_visual,
    representation_fingerprint,
    stable_artifact_id,
    system_node_fingerprint,
    walk_system_nodes,
)
from forma_core.workspaces.projects.generation_mode import is_progressive_generation


logger = logging.getLogger(__name__)


ImageProviderFactory = Callable[..., Any]
ImageStorageHandler = Callable[..., Dict[str, Any]]


def _safe_image_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        str(key): "<redacted>"
        if any(token in str(key).lower() for token in ("key", "token", "secret", "authorization"))
        else value
        for key, value in (config or {}).items()
    }


def _operation_summary(operations: list[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {"succeeded": 0, "failed": 0, "pending": 0, "not_requested": 0}
    for operation in operations:
        status = str(operation.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(operations),
        "failed": counts.get("failed", 0),
        "succeeded": counts.get("succeeded", 0),
        "pending": counts.get("pending", 0),
        "not_requested": counts.get("not_requested", 0),
        "ok": counts.get("failed", 0) == 0,
    }


def _set_operation(ir: Any, operation_id: str, **record: Any) -> None:
    metadata = dict(ir.assembly_metadata or {})
    operations = [
        item
        for item in metadata.get("operation_statuses", [])
        if isinstance(item, dict) and item.get("id") != operation_id
    ]
    operation = {"id": operation_id, **{key: value for key, value in record.items() if value is not None}}
    operations.append(operation)
    metadata["operation_statuses"] = operations
    metadata["operation_summary"] = _operation_summary(operations)
    ir.assembly_metadata = metadata


def store_project_image(
    ir: Any,
    *,
    image_data: str,
    metadata_prefix: str,
    object_prefix: str,
    fallback_content_type: str = "image/png",
    allow_remote_url: bool = False,
) -> Dict[str, Any]:
    metadata = ir.assembly_metadata or {}
    storage_config = get_image_storage_config()
    try:
        stored = upload_image_to_supabase_s3(
            image_data,
            prefix=object_prefix,
            project_id=metadata.get("project_id"),
            fallback_content_type=fallback_content_type,
            allow_remote_url=allow_remote_url,
        )
    except Exception as exc:
        logger.warning("Image persistence failed for %s: %s", metadata_prefix, exc)
        return {
            f"{metadata_prefix}_storage_error": str(exc)[:500],
            f"{metadata_prefix}_storage_bucket": storage_config.get("bucket"),
        }
    if not stored:
        return {
            f"{metadata_prefix}_storage_enabled": False,
            f"{metadata_prefix}_storage_bucket": storage_config.get("bucket"),
        }
    return {
        **stored.metadata(metadata_prefix),
        f"{metadata_prefix}_storage_enabled": True,
    }


def attach_hardware_reference_image(
    ir: Any,
    image_data: str,
    *,
    media_type: str | None = None,
    storage_handler: ImageStorageHandler = store_project_image,
) -> None:
    """Copy a user-supplied hardware reference onto assembly metadata for INFO/overview."""

    payload = str(image_data or "").strip()
    if not payload:
        return
    metadata = dict(ir.assembly_metadata or {})
    if metadata.get("reference_image_url") or metadata.get("reference_image_data"):
        return
    storage_metadata = storage_handler(
        ir,
        image_data=payload,
        metadata_prefix="reference_image",
        object_prefix="reference",
        fallback_content_type=media_type or "image/png",
        allow_remote_url=True,
    )
    reference_metadata = dict(storage_metadata)
    if not storage_metadata.get("reference_image_url"):
        reference_metadata["reference_image_data"] = payload
        if media_type:
            reference_metadata["reference_image_content_type"] = media_type
    ir.assembly_metadata = {
        **metadata,
        **reference_metadata,
        "input_mode": metadata.get("input_mode") or "prompt_image",
    }


def _visual_lifecycle_enabled(ir: Any) -> bool:
    """Use the hierarchical visual/CAD lifecycle only after explicit opt in."""

    return is_progressive_generation(ir)


def _visual_policy(ir: Any) -> VisualApprovalPolicy:
    metadata = ir.assembly_metadata or {}
    raw = metadata.get("visual_approval_policy")
    if raw:
        try:
            return VisualApprovalPolicy(str(raw))
        except ValueError:
            logger.warning("Unknown visual approval policy %r; preserving legacy auto-approval.", raw)
    return VisualApprovalPolicy.AUTO_APPROVE_VISUAL


def _safe_visual_slug(system_id: str) -> str:
    """Convert a stable system ID into a storage/view identifier."""

    return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(system_id)).strip("-") or "system"


def _system_visual_prompt(prompt_text: str, ir: Any, node: Any) -> str:
    """Create a focused concept-image prompt for one canonical system node."""

    overview = getattr(ir, "overview", None)
    project_title = str(getattr(overview, "title", "") or "Forma hardware project")
    project_description = str(getattr(overview, "description", "") or prompt_text).strip()
    responsibilities = "; ".join(getattr(node, "responsibilities", []) or []) or "not specified"
    constraints = "; ".join(getattr(node, "constraints", []) or []) or "not specified"
    roles = "; ".join(getattr(node, "expected_component_roles", []) or []) or "not specified"
    return (
        "Create a clean engineering concept render of exactly one subsystem from a larger physical hardware product. "
        "Show the subsystem as a plausible buildable physical object, isolated on a neutral background, with coherent "
        "mounting surfaces, connectors, structure, and proportions. Do not add text labels, dimensions, logos, or extra "
        "unrequested products. This image will be used as a visual design artifact for later assembly reasoning.\n\n"
        f"Overall project: {project_title}\n"
        f"Overall intent: {project_description}\n"
        f"System ID: {node.system_id}\n"
        f"Subsystem: {node.name}\n"
        f"Discipline: {node.domain}\n"
        f"Purpose: {node.purpose}\n"
        f"Responsibilities: {responsibilities}\n"
        f"Constraints: {constraints}\n"
        f"Expected physical/component roles: {roles}\n"
        f"Original user request: {prompt_text.strip()}"
    )


def _stored_image_record(
    ir: Any,
    *,
    image: Any,
    metadata_prefix: str,
    object_prefix: str,
    storage_handler: ImageStorageHandler,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Store one provider image and return its compatibility record + raw storage metadata."""

    storage_metadata = storage_handler(
        ir,
        image_data=image.data_url,
        metadata_prefix=metadata_prefix,
        object_prefix=object_prefix,
        fallback_content_type=f"image/{image.output_format or 'png'}",
        allow_remote_url=True,
    )
    image_url = storage_metadata.get(f"{metadata_prefix}_url")
    image_content_type = (
        storage_metadata.get(f"{metadata_prefix}_content_type")
        or f"image/{image.output_format or 'png'}"
    )
    record: Dict[str, Any] = {
        "view_id": image.view_id,
        "label": image.label,
        "provider": image.provider,
        "model": image.model,
        "size": image.size,
        "output_format": image.output_format,
        "model_revision": image.model_revision,
        "inference_provider": image.inference_provider,
        "model_license": image.model_license,
        "prompt": image.prompt,
        "prompt_original_length": image.prompt_original_length,
        "prompt_final_length": image.prompt_final_length,
        "prompt_compacted": image.prompt_compacted,
        "prompt_compaction_strategy": image.prompt_compaction_strategy,
        "reference_view_id": image.reference_view_id,
        "url": image_url,
        "content_type": image_content_type,
        "s3_bucket": storage_metadata.get(f"{metadata_prefix}_s3_bucket"),
        "s3_key": storage_metadata.get(f"{metadata_prefix}_s3_key"),
        "storage_method": storage_metadata.get(f"{metadata_prefix}_storage_method"),
        "storage_error": storage_metadata.get(f"{metadata_prefix}_storage_error"),
    }
    if not image_url:
        record["data"] = image.data_url
    return record, storage_metadata


def _generate_system_visuals(
    prompt_text: str,
    ir: Any,
    *,
    image_provider: Any,
    storage_handler: ImageStorageHandler,
) -> list[Dict[str, Any]]:
    """Generate/reuse one visual artifact for every non-root system node."""

    architecture = getattr(ir, "system_architecture", None)
    if architecture is None:
        return []

    lifecycle = load_design_lifecycle(ir)
    nodes = list(walk_system_nodes(architecture))
    if nodes:
        nodes = nodes[1:]
    records: list[Dict[str, Any]] = []

    for node in nodes:
        source_fingerprint = representation_fingerprint(
            prompt_text,
            system_node_fingerprint(node),
        )
        artifact_id = stable_artifact_id(node.system_id, RepresentationKind.SYSTEM_IMAGE)
        cached = lifecycle.by_id().get(artifact_id)
        if (
            cached is not None
            and cached.status == RepresentationStatus.READY
            and cached.source_fingerprint == source_fingerprint
            and cached.uri
        ):
            cached_record = dict(cached.metadata.get("image_record") or {})
            cached_record.setdefault("system_id", node.system_id)
            cached_record.setdefault("artifact_id", cached.artifact_id)
            cached_record.setdefault("url", cached.uri)
            cached_record["reused"] = True
            records.append(cached_record)
            continue

        visual_prompt = _system_visual_prompt(prompt_text, ir, node)
        try:
            generated = image_provider.generate_test_image(visual_prompt)
        except Exception as exc:
            logger.warning("Subsystem image generation failed for %s: %s", node.system_id, exc)
            records.append({
                "system_id": node.system_id,
                "name": node.name,
                "status": "failed",
                "error": str(exc)[:500],
                "source_fingerprint": source_fingerprint,
            })
            continue

        slug = _safe_visual_slug(node.system_id)
        generated.view_id = f"system-{slug}"
        generated.label = f"{node.name} concept"
        image_record, storage_metadata = _stored_image_record(
            ir,
            image=generated,
            metadata_prefix=f"system_{slug}_image",
            object_prefix=f"system-{slug}",
            storage_handler=storage_handler,
        )
        image_record.update({
            "system_id": node.system_id,
            "name": node.name,
            "status": "succeeded",
            "source_fingerprint": source_fingerprint,
            "reused": False,
        })
        uri = image_record.get("url")
        if not uri:
            uri = generated.data_url
        representation = register_system_visual(
            ir,
            node_id=node.system_id,
            source_fingerprint=source_fingerprint,
            uri=str(uri),
            metadata={
                "name": node.name,
                "domain": node.domain,
                "image_record": image_record,
                "storage": storage_metadata,
            },
        )
        image_record["artifact_id"] = representation.artifact_id
        records.append(image_record)
        lifecycle = load_design_lifecycle(ir)

    return records


def _resume_auto_approved_cad(ir: Any) -> None:
    """Continue deferred CAD after an automatically approved system render."""

    if not _visual_lifecycle_enabled(ir):
        return
    lifecycle = load_design_lifecycle(ir)
    if (
        lifecycle.visual_gate.policy != VisualApprovalPolicy.AUTO_APPROVE_VISUAL
        or lifecycle.visual_gate.status != VisualApprovalStatus.APPROVED
    ):
        return
    metadata = ir.assembly_metadata or {}
    try:
        from forma_core.workspaces.projects.cad_generation import ensure_native_cad_model

        ensure_native_cad_model(
            ir,
            project_id=str(metadata.get("project_id") or "").strip() or None,
            required=bool(metadata.get("cad_required", False)),
            authoring_agent="forma-generation-worker",
            workflow="default",
        )
    except Exception:
        if bool(metadata.get("cad_required", False)):
            raise
        logger.exception("Deferred optional CAD generation failed after visual approval.")


def attach_product_image(
    prompt_text: str,
    ir: Any,
    *,
    generate_image: bool = False,
    provider_factory: ImageProviderFactory = build_image_provider,
    storage_handler: ImageStorageHandler = store_project_image,
    settings: Optional[ResolvedIntegrationSettings] = None,
) -> None:
    """Generate product visuals, using hierarchical visuals only in progressive mode."""

    lifecycle_enabled = _visual_lifecycle_enabled(ir)
    if lifecycle_enabled:
        bootstrap_design_lifecycle(ir, policy=_visual_policy(ir))

    try:
        image_provider = provider_factory(force_enabled=generate_image, settings=settings)
    except TypeError as exc:
        if "settings" not in str(exc):
            raise
        image_provider = provider_factory(force_enabled=generate_image)
    image_config = _safe_image_config(image_provider.get_debug_config())
    status = "pending" if generate_image else "not_requested"
    visual_spec = build_project_visual_spec(prompt_text, ir)
    ir.assembly_metadata = {
        **(ir.assembly_metadata or {}),
        "image_output_requested": generate_image,
        "image_output_enabled": image_config.get("enabled", False),
        "image_output_provider": image_config.get("provider"),
        "image_output_model": image_config.get("model_name"),
        "image_output_configured": image_config.get("configured", False),
        "image_output_status": status,
        "image_output_reason": image_config.get("reason"),
        "image_output_debug": image_config,
        "product_visual_spec": visual_spec,
    }
    _set_operation(
        ir,
        "image_generation",
        label="Image generation",
        status=status,
        provider=image_config.get("provider"),
        model=image_config.get("model_name"),
        requested=generate_image,
        enabled=image_config.get("enabled", False),
        configured=image_config.get("configured", False),
        reason=image_config.get("reason"),
    )
    if not generate_image:
        return
    if not image_config.get("configured", False):
        error = image_config.get("reason") or "Image output was requested, but the provider is not configured."
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": str(error)[:500],
            "image_output_error_type": "configuration",
            "product_image_error": str(error)[:500],
        }
        _set_operation(
            ir,
            "image_generation",
            label="Image generation",
            status="failed",
            requested=True,
            configured=False,
            error=str(error)[:500],
            error_type="configuration",
        )
        return

    system_visuals: list[Dict[str, Any]] = []
    if lifecycle_enabled:
        _set_operation(
            ir,
            "system_visual_generation",
            label="Subsystem visual generation",
            status="pending",
            requested=True,
            configured=True,
            provider=image_config.get("provider"),
            model=image_config.get("model_name"),
        )
        system_visuals = _generate_system_visuals(
            prompt_text,
            ir,
            image_provider=image_provider,
            storage_handler=storage_handler,
        )
        succeeded_system_visuals = [item for item in system_visuals if item.get("status") == "succeeded"]
        failed_system_visuals = [item for item in system_visuals if item.get("status") == "failed"]
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "system_visuals": system_visuals,
            "system_visual_count": len(system_visuals),
            "system_visual_succeeded_count": len(succeeded_system_visuals),
            "system_visual_failed_count": len(failed_system_visuals),
        }
        _set_operation(
            ir,
            "system_visual_generation",
            label="Subsystem visual generation",
            status="failed" if failed_system_visuals and not succeeded_system_visuals else "succeeded",
            requested=True,
            configured=True,
            provider=image_config.get("provider"),
            model=image_config.get("model_name"),
            details={
                "requested_count": len(system_visuals),
                "succeeded_count": len(succeeded_system_visuals),
                "failed_count": len(failed_system_visuals),
            },
        )

    try:
        generated_images = image_provider.generate_project_image_sequence(prompt_text, ir)
    except Exception as exc:
        logger.exception("Image generation failed: %s", exc)
        error = str(exc)[:500]
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": error,
            "image_output_error_type": exc.__class__.__name__,
            "product_image_error": error,
        }
        _set_operation(
            ir,
            "image_generation",
            label="Image generation",
            status="failed",
            requested=True,
            error=error,
            error_type=exc.__class__.__name__,
        )
        return

    if not generated_images:
        error = "Image provider returned no images."
        ir.assembly_metadata = {
            **(ir.assembly_metadata or {}),
            "image_output_status": "failed",
            "image_output_failed": True,
            "image_output_error": error,
            "image_output_error_type": "empty_response",
            "product_image_error": error,
            "product_visual_sequence_count": 0,
        }
        _set_operation(ir, "image_generation", label="Image generation", status="failed", error=error)
        return

    product_metadata: Dict[str, Any] = {
        "image_output_status": "succeeded",
        "image_output_failed": False,
        "image_output_error": None,
        "image_output_error_type": None,
        "image_output_generated_count": len(generated_images),
        "product_image_error": None,
        "product_visual_sequence_count": len(generated_images),
    }
    sequence: list[Dict[str, Any]] = []
    system_render_representation = None
    for index, image in enumerate(generated_images):
        view_id = image.view_id or f"view_{index + 1}"
        image.view_id = view_id
        metadata_prefix = f"product_{view_id}_image"
        image_record, storage_metadata = _stored_image_record(
            ir,
            image=image,
            metadata_prefix=metadata_prefix,
            object_prefix=f"product-{view_id}",
            storage_handler=storage_handler,
        )
        image_url = image_record.get("url")
        image_content_type = image_record.get("content_type")
        if not image_url:
            product_metadata[f"{metadata_prefix}_data"] = image.data_url
        sequence.append(image_record)
        product_metadata.update(storage_metadata)
        if index == 0:
            for field in (
                "provider", "model", "size", "output_format", "model_revision",
                "inference_provider", "model_license", "prompt", "prompt_original_length",
                "prompt_final_length", "prompt_compacted", "prompt_compaction_strategy",
            ):
                product_metadata[f"product_image_{field}"] = getattr(image, field)
            product_metadata.update({
                "product_image_url": image_url,
                "product_image_content_type": image_content_type,
                "product_image_s3_bucket": storage_metadata.get(f"{metadata_prefix}_s3_bucket"),
                "product_image_s3_key": storage_metadata.get(f"{metadata_prefix}_s3_key"),
                "product_image_storage_method": storage_metadata.get(f"{metadata_prefix}_storage_method"),
            })
            if not image_url:
                product_metadata["product_image_data"] = image.data_url

            if lifecycle_enabled:
                lifecycle = load_design_lifecycle(ir)
                visual_dependencies = [
                    str(item.get("artifact_id"))
                    for item in system_visuals
                    if item.get("artifact_id") and item.get("status") == "succeeded"
                ]
                system_render_representation = register_system_render(
                    ir,
                    source_fingerprint=representation_fingerprint(
                        prompt_text,
                        visual_spec,
                        [
                            (item.get("system_id"), item.get("source_fingerprint"))
                            for item in system_visuals
                            if item.get("status") == "succeeded"
                        ],
                        image.prompt,
                    ),
                    uri=str(image_url or image.data_url),
                    depends_on=visual_dependencies,
                    metadata={
                        "image_record": image_record,
                        "storage": storage_metadata,
                        "provider": image.provider,
                        "model": image.model,
                    },
                )
                lifecycle = load_design_lifecycle(ir)
                product_metadata.update({
                    "system_render_artifact_id": system_render_representation.artifact_id,
                    "visual_approval_policy": lifecycle.visual_gate.policy.value,
                    "visual_approval_status": lifecycle.visual_gate.status.value,
                })

    product_metadata["product_visual_sequence"] = sequence
    ir.assembly_metadata = {**(ir.assembly_metadata or {}), **product_metadata}
    storage_errors = [item.get("storage_error") for item in sequence if item.get("storage_error")]
    _set_operation(
        ir,
        "image_generation",
        label="Image generation",
        status="succeeded",
        provider=image_config.get("provider"),
        model=image_config.get("model_name"),
        requested=True,
        enabled=True,
        configured=True,
        details={
            "generated_count": len(generated_images),
            "system_visual_count": len(system_visuals),
            "system_render_artifact_id": (
                system_render_representation.artifact_id if system_render_representation is not None else None
            ),
        },
    )
    _set_operation(
        ir,
        "image_storage",
        label="Image storage",
        status="failed" if storage_errors else "succeeded",
        requested=True,
        enabled=bool(get_image_storage_config().get("enabled")),
        configured=True,
        error=str(storage_errors[0])[:500] if storage_errors else None,
        details={
            "stored_count": len([item for item in sequence if item.get("url")]),
            "inline_count": len([item for item in sequence if item.get("data")]),
        },
    )

    _resume_auto_approved_cad(ir)


def attach_assembly_step(ir: Any, filepath: str | Path) -> Dict[str, Any]:
    """Attach a validated native STEP artifact to a generated project."""
    path = Path(filepath).expanduser().resolve()
    if path.suffix.lower() not in {".step", ".stp"}:
        raise ValueError("Assembly artifact must end in .step or .stp.")
    if not path.is_file():
        raise ValueError(f"Assembly artifact does not exist: {path}")

    data = path.read_bytes()
    upper = data.upper()
    if (
        not upper.lstrip().startswith(b"ISO-10303-21;")
        or b"HEADER;" not in upper
        or b"DATA;" not in upper
        or b"END-ISO-10303-21;" not in upper
    ):
        raise ValueError(f"Assembly artifact is not a valid STEP exchange file: {path}")

    artifact = {
        "path": str(path),
        "filename": path.name,
        "format": "step",
        "units": "mm",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    current = ir.cad_model if isinstance(ir.cad_model, dict) else {}
    ir.cad_model = {
        **current,
        "adapter": "forma-opencad",
        "source": "CLI assembly STEP artifact",
        **artifact,
    }
    return artifact


def persist_project_output(ir: Any, *, prompt_text: str = "", owner_user_id: Optional[str] = None) -> str:
    """Update the existing generated project, or save it when generation did not."""
    init_db()
    metadata = ir.assembly_metadata or {}
    project_id = metadata.get("project_id")
    if not project_id:
        raise ValueError("Project output cannot be persisted without assembly_metadata.project_id.")
    hardware_ir = ir.model_dump(mode="json")
    if owner_user_id:
        claim_unowned_generated_project(project_id, hardware_ir, owner_user_id)
        persist_chat_project_revision(
            project_id,
            owner_user_id,
            ir,
            source_job_id=f"project-output-{uuid.uuid4().hex}",
            prompt=prompt_text.strip(),
            chat_id=metadata.get("chat_id"),
        )
        return project_id
    if refresh_legacy_project_projection(project_id, hardware_ir, owner_user_id=owner_user_id):
        return project_id
    title = getattr(getattr(ir, "overview", None), "title", None) or prompt_text.strip() or "Untitled Forma Project"
    created_at = metadata.get("created_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    persist_legacy_project_projection(
        project_id=project_id,
        title=title,
        prompt=prompt_text.strip(),
        hardware_ir=hardware_ir,
        created_at=created_at,
        chat_id=metadata.get("chat_id"),
        owner_user_id=owner_user_id,
        visibility="public",
    )
    return project_id


def primary_product_image_data(ir: Any) -> Optional[str]:
    metadata = ir.assembly_metadata or {}
    value = metadata.get("product_image_data")
    return value if isinstance(value, str) and value else None


__all__ = [
    "attach_hardware_reference_image",
    "attach_assembly_step",
    "attach_product_image",
    "persist_project_output",
    "primary_product_image_data",
    "store_project_image",
]
