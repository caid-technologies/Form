from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.auth import UserContext, require_user_context
from apps.api.context_builds import ContextBuildDispatcher
from apps.api.hosted_chat import require_hosted_chat_enabled
from forma_core.database import (
    cancel_project_generation_plan,
    get_project_generation_plan,
    reset_project_generation_plan,
)
from forma_core.workers import WorkerExecutionPlan, WorkerPlanningError


router = APIRouter(prefix="/projects/{project_id}/build", tags=["project-build-execution"])


def _owner(user: UserContext) -> str:
    owner = str(user.owner_user_id or "").strip()
    if not owner:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to view build execution.")
    return owner


@router.get("/plans/{plan_id}", response_model=WorkerExecutionPlan)
def get_build_plan_endpoint(
    project_id: UUID,
    plan_id: str,
    user: UserContext = Depends(require_user_context),
) -> WorkerExecutionPlan:
    try:
        plan = get_project_generation_plan(plan_id, _owner(user))
    except WorkerPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.as_dict()) from exc
    if str(plan.project_id) != str(project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build execution not found.")
    return plan


@router.post("/plans/{plan_id}/execute", response_model=WorkerExecutionPlan)
async def execute_build_plan_endpoint(
    project_id: UUID,
    plan_id: str,
    user: UserContext = Depends(require_user_context),
) -> WorkerExecutionPlan:
    """Execute a plan inside this request so serverless runtimes keep it alive."""

    require_hosted_chat_enabled(user)
    owner = _owner(user)
    try:
        plan = get_project_generation_plan(plan_id, owner)
    except WorkerPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.as_dict()) from exc
    if str(plan.project_id) != str(project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build execution not found.")
    return await ContextBuildDispatcher.execute(plan_id, owner)


@router.post("/plans/{plan_id}/cancel", response_model=WorkerExecutionPlan)
async def cancel_build_plan_endpoint(
    project_id: UUID,
    plan_id: str,
    user: UserContext = Depends(require_user_context),
) -> WorkerExecutionPlan:
    owner = _owner(user)
    try:
        plan = get_project_generation_plan(plan_id, owner)
    except WorkerPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.as_dict()) from exc
    if str(plan.project_id) != str(project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build execution not found.")
    ContextBuildDispatcher.signal_cancel(plan_id)
    return await cancel_project_generation_plan(plan_id, owner)


@router.post("/plans/{plan_id}/reset", response_model=WorkerExecutionPlan)
async def reset_build_plan_endpoint(
    project_id: UUID,
    plan_id: str,
    user: UserContext = Depends(require_user_context),
) -> WorkerExecutionPlan:
    """Reset a failed generation plan so the current user can try it again."""

    require_hosted_chat_enabled(user)
    owner = _owner(user)
    try:
        plan = get_project_generation_plan(plan_id, owner)
    except WorkerPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.as_dict()) from exc
    if str(plan.project_id) != str(project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Build execution not found.")
    try:
        return await reset_project_generation_plan(plan_id, owner)
    except WorkerPlanningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.as_dict()) from exc


__all__ = ["router"]
