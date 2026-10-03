from __future__ import annotations

import logging
from forma_core.config import config
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forma_core.user_integrations import (
    UserIntegrationStore,
    resolve_user_integration_settings,
    default_integration_store,
    integration_status_payload,
)
from apps.api.auth import UserContext, require_user_context
from forma_core.debug import api_error_detail, redact_debug_text, redact_debug_value
from forma_core.images import build_image_provider
from forma_core.runtime import deployment_mode_enabled


router = APIRouter(prefix="/user", tags=["user"])
logger = logging.getLogger(__name__)


class IntegrationUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    fields: dict[str, Optional[str]] = Field(default_factory=dict)
    clear_fields: list[str] = Field(default_factory=list)


class ImageModelTestRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1, max_length=2000)


def image_model_test_available() -> bool:
    vercel_env = (config.get("VERCEL_ENV") or "").strip().lower()
    if vercel_env in {"preview", "development"}:
        return True
    if vercel_env == "production" or config.get("VERCEL") == "1":
        return False
    return not deployment_mode_enabled()


def _status_payload(store: UserIntegrationStore) -> dict[str, object]:
    payload = integration_status_payload(store)
    payload["image_model_test_available"] = image_model_test_available()
    return payload


def _store_for_context(user_context: UserContext) -> UserIntegrationStore:
    if user_context.provider == "local":
        return default_integration_store()
    owner_user_id = user_context.owner_user_id
    if not owner_user_id:
        raise HTTPException(status_code=401, detail="Sign in to manage provider settings.")
    return UserIntegrationStore.for_user(owner_user_id)


def _storage_label(store: UserIntegrationStore) -> str:
    return str(getattr(store, "storage_label", getattr(store, "path", "unknown")))


def _unexpected_storage_error(
    *,
    operation: str,
    store: UserIntegrationStore,
    user_context: UserContext,
    exc: Exception,
    integration_id: Optional[str] = None,
) -> HTTPException:
    owner_user_id = user_context.owner_user_id
    logger.exception(
        "User integration %s failed: owner_user_id=%s integration_id=%s storage=%s error_type=%s",
        operation,
        owner_user_id,
        integration_id,
        _storage_label(store),
        type(exc).__name__,
    )
    cause = redact_debug_text(str(exc)).strip()
    message = f"Provider settings {operation} failed"
    if cause:
        message = f"{message}: {cause}"
    return HTTPException(
        status_code=500,
        detail=api_error_detail(
            code=f"user_integrations_{operation}_failed",
            message=message,
            exc=exc,
            context={
                "owner_user_id": owner_user_id,
                "integration_id": integration_id,
                "storage": _storage_label(store),
                "operation": operation,
            },
        ),
    )


@router.get("/integrations")
def get_user_integrations(user_context: UserContext = Depends(require_user_context)) -> dict[str, object]:
    store = _store_for_context(user_context)
    try:
        return _status_payload(store)
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="load",
            store=store,
            user_context=user_context,
            exc=exc,
        ) from exc


@router.post("/integrations/reload")
def reload_user_integrations(user_context: UserContext = Depends(require_user_context)) -> dict[str, object]:
    store = _store_for_context(user_context)
    try:
        return _status_payload(store)
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="reload",
            store=store,
            user_context=user_context,
            exc=exc,
        ) from exc


@router.put("/integrations/{integration_id}")
def update_user_integration(
    integration_id: str,
    request: IntegrationUpdateRequest,
    user_context: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    store = _store_for_context(user_context)
    try:
        store.update_integration(
            integration_id,
            enabled=request.enabled,
            field_values=request.fields,
            clear_fields=request.clear_fields,
        )
    except ValueError as exc:
        logger.warning(
            "User integration update rejected: owner_user_id=%s integration_id=%s storage=%s error=%s",
            user_context.owner_user_id,
            integration_id,
            _storage_label(store),
            redact_debug_text(str(exc)),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        logger.warning(
            "User integration update target not found: owner_user_id=%s integration_id=%s storage=%s error=%s",
            user_context.owner_user_id,
            integration_id,
            _storage_label(store),
            redact_debug_text(str(exc)),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="save",
            store=store,
            user_context=user_context,
            exc=exc,
            integration_id=integration_id,
        ) from exc
    logger.info(
        "User integration update persisted: owner_user_id=%s integration_id=%s storage=%s "
        "enabled=%s field_ids=%s clear_fields=%s",
        user_context.owner_user_id,
        integration_id,
        _storage_label(store),
        request.enabled,
        sorted(request.fields),
        sorted(request.clear_fields),
    )
    try:
        return _status_payload(store)
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="post_save_reload",
            store=store,
            user_context=user_context,
            exc=exc,
            integration_id=integration_id,
        ) from exc


@router.delete("/integrations/{integration_id}")
def clear_user_integration(
    integration_id: str,
    user_context: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    store = _store_for_context(user_context)
    try:
        store.clear_integration(integration_id)
    except KeyError as exc:
        logger.warning(
            "User integration clear target not found: owner_user_id=%s integration_id=%s storage=%s error=%s",
            user_context.owner_user_id,
            integration_id,
            _storage_label(store),
            redact_debug_text(str(exc)),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="clear",
            store=store,
            user_context=user_context,
            exc=exc,
            integration_id=integration_id,
        ) from exc
    logger.info(
        "User integration clear persisted: owner_user_id=%s integration_id=%s storage=%s",
        user_context.owner_user_id,
        integration_id,
        _storage_label(store),
    )
    try:
        return _status_payload(store)
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="post_clear_reload",
            store=store,
            user_context=user_context,
            exc=exc,
            integration_id=integration_id,
        ) from exc


@router.post("/integrations/image-model-test")
def test_image_model(
    request: ImageModelTestRequest,
    user_context: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    if not image_model_test_available():
        raise HTTPException(status_code=404, detail="Image model testing is only available locally and in preview deployments.")

    store = _store_for_context(user_context)
    try:
        settings = resolve_user_integration_settings(store, fail_open=False)
    except Exception as exc:
        raise _unexpected_storage_error(
            operation="image_model_test_load",
            store=store,
            user_context=user_context,
            exc=exc,
        ) from exc

    provider = build_image_provider(force_enabled=True, settings=settings)
    config = redact_debug_value(provider.get_debug_config())
    actual_provider = str(getattr(provider, "provider_name", "") or "")
    actual_model = str(getattr(provider, "model_name", "") or "")
    requested_provider = request.provider.strip().lower().replace("_", "-")
    requested_model = request.model.strip()

    if actual_provider != requested_provider or actual_model != requested_model:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "image_model_test_settings_not_saved",
                "message": (
                    f"Saved image settings resolve to {actual_provider or 'none'}/{actual_model or 'none'}, "
                    f"not {requested_provider}/{requested_model}. Save the current image settings before testing."
                ),
                "provider": actual_provider,
                "model": actual_model,
                "config": config,
            },
        )

    if not bool(config.get("configured")):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "image_model_test_not_configured",
                "message": str(config.get("reason") or "The selected image provider is not configured."),
                "provider": actual_provider,
                "model": actual_model,
                "config": config,
            },
        )

    started_at = time.monotonic()
    try:
        image = provider.generate_test_image(request.prompt.strip())
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        logger.exception(
            "Direct image model test failed: owner_user_id=%s provider=%s model=%s elapsed_ms=%s error_type=%s",
            user_context.owner_user_id,
            actual_provider,
            actual_model,
            elapsed_ms,
            type(exc).__name__,
        )
        detail = api_error_detail(
            code="image_model_test_failed",
            message=f"{type(exc).__name__}: {redact_debug_text(str(exc))}",
            exc=exc,
            provider=actual_provider,
            model=actual_model,
            context={"elapsed_ms": elapsed_ms, "config": config},
        )
        detail.update({"elapsed_ms": elapsed_ms, "config": config, "error_type": type(exc).__name__})
        raise HTTPException(status_code=502, detail=detail) from exc

    elapsed_ms = round((time.monotonic() - started_at) * 1000)
    return {
        "ok": True,
        "provider": image.provider,
        "model": image.model,
        "size": image.size,
        "output_format": image.output_format,
        "elapsed_ms": elapsed_ms,
        "prompt": image.prompt,
        "prompt_original_length": image.prompt_original_length,
        "prompt_final_length": image.prompt_final_length,
        "prompt_compacted": image.prompt_compacted,
        "image_data_url": image.data_url,
        "config": config,
    }
