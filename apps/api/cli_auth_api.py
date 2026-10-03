"""Device authorization and account endpoints for the Forma OSS CLI."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.api.auth import UserContext, require_user_context
from apps.api.cli_auth_store import (
    CliIdentity,
    approve_device,
    create_device_session,
    deny_device,
    device_status,
    exchange_device,
    refresh_access,
    revoke_tokens,
)
from forma_core.config import config


router = APIRouter(prefix="/cli", tags=["cli"])


class DeviceAuthorizationRequest(BaseModel):
    client_name: str = Field(default="forma-oss", max_length=100)


class DeviceCodeRequest(BaseModel):
    device_code: str = Field(min_length=1, max_length=200)


class UserCodeRequest(BaseModel):
    user_code: str = Field(min_length=3, max_length=20)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=500)


def _configured_web_url() -> Optional[str]:
    value = (
        config.optional("FORMA_WEB_URL")
        or config.optional("NEXT_PUBLIC_APP_URL")
        or config.optional("FRONTEND_URL")
    )
    return value.rstrip("/") if value else None


def _request_origin(request: Request) -> Optional[str]:
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",", 1)[0].strip()
    if not host:
        return None
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip()
    return f"{scheme}://{host}".rstrip("/")


def _web_url(request: Optional[Request] = None) -> str:
    configured = _configured_web_url()
    if configured:
        return configured
    if request is not None and (config.optional("FORMA_DEPLOYMENT_MODE") or "local").lower() == "hosted":
        origin = _request_origin(request)
        if origin:
            return origin
    return "http://127.0.0.1:3000"


def _api_url(request: Optional[Request] = None) -> str:
    configured = config.optional("FORMA_API_URL") or config.optional("NEXT_PUBLIC_API_URL")
    if configured:
        return configured.rstrip("/")
    if request is not None and (config.optional("FORMA_DEPLOYMENT_MODE") or "local").lower() == "hosted":
        origin = _request_origin(request)
        if origin:
            return f"{origin}/api"
    return "http://127.0.0.1:8000"


@router.post("/device/authorize")
def authorize_device(
    request: DeviceAuthorizationRequest,
    http_request: Request,
) -> dict[str, object]:
    session = create_device_session()
    return {
        "device_code": session.device_code,
        "user_code": session.user_code,
        "verification_uri": f"{_web_url(http_request)}/cli/authorize?code={session.user_code}",
        "expires_in": max(1, round(session.expires_at - time.time())),
        "interval": 5,
        "client_name": request.client_name,
    }


@router.post("/device/poll")
def poll_device(request: DeviceCodeRequest) -> dict[str, object]:
    current = device_status(request.device_code)
    if current == "pending":
        return {"status": "authorization_pending", "interval": 5}
    if current == "approved":
        return {"status": "approved"}
    return {"status": current, "message": f"Device authorization is {current}."}


@router.post("/device/approve")
async def approve_device_endpoint(
    request: UserCodeRequest,
    user: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    if not user.owner_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in before approving a CLI session.")
    claims = user.claims
    identity = CliIdentity(
        subject=user.owner_user_id,
        provider=user.provider,
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        display_name=claims.get("name") if isinstance(claims.get("name"), str) else None,
    )
    if not approve_device(request.user_code, identity):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CLI authorization code is invalid or expired.")
    return {"ok": True, "status": "approved"}


@router.post("/device/deny")
async def deny_device_endpoint(
    request: UserCodeRequest,
    _user: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    if not deny_device(request.user_code):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CLI authorization code is invalid or expired.")
    return {"ok": True, "status": "denied"}


@router.post("/device/token")
def exchange_device_token(request: DeviceCodeRequest) -> dict[str, object]:
    issued = exchange_device(request.device_code)
    if issued is None:
        current = device_status(request.device_code)
        detail = "CLI authorization is not approved." if current == "pending" else "CLI authorization code is invalid or expired."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    access_token, refresh_token, expires_in = issued
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }


@router.post("/token/refresh")
def refresh_token(request: RefreshTokenRequest) -> dict[str, object]:
    issued = refresh_access(request.refresh_token)
    if issued is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token is invalid or expired.")
    access_token, refresh_token_value, expires_in = issued
    return {
        "access_token": access_token,
        "refresh_token": refresh_token_value,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }


@router.post("/token/revoke")
async def revoke_token(request: Request, body: Optional[RefreshTokenRequest] = None) -> dict[str, object]:
    authorization = request.headers.get("authorization", "")
    _, _, access_token = authorization.partition(" ")
    revoke_tokens(
        refresh_token=body.refresh_token if body else None,
        access_token=access_token.strip() or None,
    )
    return {"ok": True}


@router.get("/whoami")
async def cli_whoami(
    request: Request,
    user: UserContext = Depends(require_user_context),
) -> dict[str, object]:
    claims = user.claims
    return {
        "subject": user.owner_user_id or user.subject or "unknown",
        "provider": user.provider,
        "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
        "display_name": claims.get("name") if isinstance(claims.get("name"), str) else None,
        "api_url": _api_url(request),
    }


__all__ = ["router"]
