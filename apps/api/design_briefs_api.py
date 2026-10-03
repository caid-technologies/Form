from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, status

from apps.api.auth import UserContext, require_user_context
from apps.api.hosted_chat import require_hosted_chat_enabled
from forma_core.database import (
    DesignBriefAccessError,
    DesignBriefNotFoundError,
    create_design_brief_version,
    get_design_brief_version,
    get_latest_design_brief,
    list_design_brief_versions,
)
from forma_core.workspaces.design_briefs import (
    DesignBrief,
    DesignBriefCreate,
    DesignBriefVersionList,
)


router = APIRouter(prefix="/projects/{project_id}/design-briefs", tags=["design-briefs"])


def _owner_user_id(user_context: UserContext) -> str:
    owner_user_id = str(user_context.owner_user_id or "").strip()
    if not owner_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required", "message": "Sign in to manage design briefs."},
        )
    return owner_user_id


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "design_brief_not_found", "message": "DesignBrief not found."},
    )


@router.post("", response_model=DesignBrief, status_code=status.HTTP_201_CREATED)
def create_design_brief_endpoint(
    project_id: UUID,
    request: DesignBriefCreate,
    user_context: UserContext = Depends(require_user_context),
) -> DesignBrief:
    require_hosted_chat_enabled(user_context)
    try:
        return create_design_brief_version(
            str(project_id),
            _owner_user_id(user_context),
            request,
        )
    except DesignBriefAccessError as exc:
        raise _not_found() from exc


@router.get("", response_model=DesignBriefVersionList)
def list_design_briefs_endpoint(
    project_id: UUID,
    user_context: UserContext = Depends(require_user_context),
) -> DesignBriefVersionList:
    versions = list_design_brief_versions(str(project_id), _owner_user_id(user_context))
    return DesignBriefVersionList(project_id=project_id, versions=versions)


@router.get("/latest", response_model=DesignBrief)
def get_latest_design_brief_endpoint(
    project_id: UUID,
    user_context: UserContext = Depends(require_user_context),
) -> DesignBrief:
    try:
        return get_latest_design_brief(str(project_id), _owner_user_id(user_context))
    except DesignBriefNotFoundError as exc:
        raise _not_found() from exc


@router.get("/{brief_version}", response_model=DesignBrief)
def get_design_brief_version_endpoint(
    project_id: UUID,
    brief_version: int = Path(ge=1),
    user_context: UserContext = Depends(require_user_context),
) -> DesignBrief:
    try:
        return get_design_brief_version(
            str(project_id),
            _owner_user_id(user_context),
            brief_version,
        )
    except DesignBriefNotFoundError as exc:
        raise _not_found() from exc
