"""Public hosted compatibility metadata for Forma clients."""

from __future__ import annotations

from fastapi import APIRouter

from forma_core.config.compatibility import CompatibilityMetadata, hosted_compatibility_metadata


router = APIRouter(tags=["compatibility"])


@router.get("/forma/version", response_model=CompatibilityMetadata)
def forma_version() -> CompatibilityMetadata:
    return hosted_compatibility_metadata()


__all__ = ["router"]
