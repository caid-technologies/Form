"""Server-side compatibility checks for CLI/SDK requests."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from forma_core._version import __version__
from forma_core.config.compatibility import (
    CURRENT_PROTOCOL_VERSION,
    compatibility_error_detail,
    evaluate_compatibility,
    hosted_compatibility_metadata,
)


def require_client_compatibility(request: Request) -> None:
    """Reject explicitly identified clients that cannot use the hosted contract.

    Missing headers remain permissive for older clients so the hosted service can
    roll out the gate without making an otherwise valid legacy client fail before
    it has an opportunity to upgrade.
    """

    client_version = request.headers.get("x-forma-client-version")
    client_protocol = request.headers.get("x-forma-protocol-version")
    if not client_version and not client_protocol:
        return
    try:
        protocol_version = int(client_protocol or CURRENT_PROTOCOL_VERSION)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail={
                "code": "UNSUPPORTED_PROTOCOL_VERSION",
                "message": "X-Forma-Protocol-Version must be an integer.",
                "protocol_version": hosted_compatibility_metadata().protocol_version,
            },
        ) from None
    result = evaluate_compatibility(
        client_version or __version__,
        hosted_compatibility_metadata(),
        client_protocol_version=protocol_version,
    )
    if result.is_blocking:
        raise HTTPException(
            status_code=status.HTTP_426_UPGRADE_REQUIRED,
            detail=compatibility_error_detail(result),
        )


__all__ = ["require_client_compatibility"]
