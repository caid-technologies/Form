from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.auth import UserContext, require_user_context
from apps.api.hosted_chat import require_hosted_chat_enabled
from forma_core.database import (
    get_project_workflow,
    initialize_project_workflow,
    list_project_workflow_transitions,
    transition_project_workflow,
)
from forma_core.workspaces.workflow import (
    ProjectWorkflow,
    ProjectWorkflowHistory,
    WorkflowActorType,
    WorkflowStateError,
    WorkflowTransitionCommand,
    WorkflowTransitionOutcome,
)


router = APIRouter(prefix="/projects/{project_id}/workflow", tags=["project-workflow"])


def _owner(user: UserContext) -> str:
    owner_user_id = str(user.owner_user_id or "").strip()
    if not owner_user_id:
        raise HTTPException(status_code=401, detail={"code": "authentication_required", "message": "Sign in."})
    return owner_user_id


def _workflow_error(exc: WorkflowStateError) -> HTTPException:
    status_code = 404 if exc.code == "workflow_not_found" else status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail=exc.as_dict())


@router.post("", response_model=WorkflowTransitionOutcome)
def initialize_workflow_endpoint(
    project_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> WorkflowTransitionOutcome:
    require_hosted_chat_enabled(user)
    try:
        owner = _owner(user)
        return initialize_project_workflow(
            str(project_id),
            owner,
            actor_type=WorkflowActorType.USER,
            actor_id=owner,
            reason="User started a project conversation.",
        )
    except WorkflowStateError as exc:
        raise _workflow_error(exc) from exc


@router.get("", response_model=ProjectWorkflow)
def get_workflow_endpoint(
    project_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> ProjectWorkflow:
    try:
        return get_project_workflow(str(project_id), _owner(user))
    except WorkflowStateError as exc:
        raise _workflow_error(exc) from exc


@router.get("/transitions", response_model=ProjectWorkflowHistory)
def get_workflow_history_endpoint(
    project_id: UUID,
    user: UserContext = Depends(require_user_context),
) -> ProjectWorkflowHistory:
    try:
        owner = _owner(user)
        workflow = get_project_workflow(str(project_id), owner)
        transitions = list_project_workflow_transitions(str(project_id), owner)
        return ProjectWorkflowHistory(workflow=workflow, transitions=transitions)
    except WorkflowStateError as exc:
        raise _workflow_error(exc) from exc


@router.post("/transitions", response_model=WorkflowTransitionOutcome)
def transition_workflow_endpoint(
    project_id: UUID,
    command: WorkflowTransitionCommand,
    user: UserContext = Depends(require_user_context),
) -> WorkflowTransitionOutcome:
    require_hosted_chat_enabled()
    try:
        owner = _owner(user)
        return transition_project_workflow(
            str(project_id),
            owner,
            command.to_state,
            actor_type=WorkflowActorType.USER,
            actor_id=owner,
            reason=command.reason,
            idempotency_key=command.idempotency_key,
        )
    except WorkflowStateError as exc:
        raise _workflow_error(exc) from exc
