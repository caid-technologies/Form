"""CLI-facing managed credential API that never returns raw secret values."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apps.api.auth import UserContext, require_user_context
from forma_core.user_integrations import (
    UserIntegrationStore,
    integration_definition_by_id,
    integration_status_payload,
)


router = APIRouter(prefix="/cli/credentials", tags=["cli"])


class CredentialUpdateRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=1000)
    label: str | None = Field(default=None, max_length=120)


def _store(user: UserContext) -> UserIntegrationStore:
    if not user.owner_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to manage credentials.")
    return UserIntegrationStore.for_user(user.owner_user_id)


def _credential_metadata(item: dict[str, Any]) -> dict[str, Any] | None:
    secret_fields = [field for field in item.get("fields", []) if field.get("secret")]
    if not secret_fields:
        return None
    secret = secret_fields[0]
    if not secret.get("saved"):
        return None
    return {
        "provider": item.get("id"),
        "credential_id": f"forma-cli-{item.get('id')}-api-key",
        "label": item.get("label") or item.get("id"),
        "masked_value": secret.get("masked_value"),
        "updated_at": item.get("updated_at"),
        "configured": True,
    }


@router.get("")
async def list_cli_credentials(user: UserContext = Depends(require_user_context)) -> dict[str, object]:
    payload = integration_status_payload(_store(user))
    items = [
        metadata
        for item in payload.get("integrations", [])
        if isinstance(item, dict)
        for metadata in [_credential_metadata(item)]
        if metadata is not None
    ]
    return {"items": items}


@router.post("")
async def set_cli_credential(
    request: CredentialUpdateRequest,
    user: UserContext = Depends(require_user_context),
) -> dict[str, Any]:
    provider = request.provider.strip().lower().replace("_", "-")
    try:
        definition = integration_definition_by_id(provider)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider.") from exc
    secret_fields = [field for field in definition.fields if field.secret]
    if not secret_fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provider has no managed credential field.")
    _store(user).update_integration(
        provider,
        enabled=True,
        field_values={secret_fields[0].id: request.value},
    )
    return {
        "provider": provider,
        "credential_id": f"forma-cli-{provider}-api-key",
        "label": request.label or provider,
        "configured": True,
    }


@router.delete("/{provider}")
async def remove_cli_credential(
    provider: str,
    user: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    normalized = provider.strip().lower().replace("_", "-")
    try:
        integration_definition_by_id(normalized)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown provider.") from exc
    _store(user).clear_integration(normalized)
    return {"ok": True, "provider": normalized}


__all__ = ["router"]
