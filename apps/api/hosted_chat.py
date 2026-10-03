from fastapi import HTTPException, status

from apps.api.auth import UserContext, has_opencode_authoring_access
from forma_core.config.runtime import (
    HOSTED_CHAT_UNAVAILABLE_MESSAGE,
    HostedChatUnavailableError,
    ensure_hosted_chat_enabled,
)


HOSTED_CHAT_UNAVAILABLE_CODE = "hosted_chat_unavailable"


def require_hosted_chat_enabled(
    user: UserContext | None = None,
) -> None:
    """Allow hosted chat globally or for signed-in authoring users."""

    try:
        ensure_hosted_chat_enabled()
        return
    except HostedChatUnavailableError:
        pass

    if has_opencode_authoring_access(user):
        return

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": HOSTED_CHAT_UNAVAILABLE_CODE,
            "message": HOSTED_CHAT_UNAVAILABLE_MESSAGE,
        },
    )
