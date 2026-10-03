from __future__ import annotations

import asyncio
import logging
from threading import Event, Lock, Thread

from forma_core.database import (
    create_project_generation_plan,
    evaluate_project_readiness,
    execute_project_generation_plan,
    get_project_generation_plan,
    initiate_project_build,
)
from forma_core.config import config
from forma_core.workspaces.context import ContextBuildExecution
from forma_core.workspaces.readiness import BuildMode, ReadinessStatus
from forma_core.workspaces.workflow import ProjectWorkflow
from forma_core.vertex_auth import (
    bind_vertex_oidc_token,
    current_vertex_oidc_token,
    reset_vertex_oidc_token,
)


logger = logging.getLogger(__name__)


DEFAULT_AGENT_ASSUMPTION = (
    "Build agents may resolve the remaining choices using safe prototype defaults "
    "and must record those choices as assumptions."
)


class ContextBuildDispatcher:
    """Turn conversational permission to proceed into a durable worker execution plan."""

    _cancellation_events: dict[str, Event] = {}
    _cancellation_lock = Lock()

    def start(
        self,
        project_id: str,
        owner_user_id: str,
        conversation_id: str,
        *,
        generation_mode: str = "regular",
    ) -> tuple[ContextBuildExecution, ProjectWorkflow]:
        readiness = evaluate_project_readiness(project_id, owner_user_id)
        mode = BuildMode.BUILD if readiness.status == ReadinessStatus.READY else BuildMode.BUILD_ANYWAY
        assumptions = [] if mode == BuildMode.BUILD else [DEFAULT_AGENT_ASSUMPTION]
        outcome = initiate_project_build(
            project_id,
            owner_user_id,
            mode=mode,
            actor_id=owner_user_id,
            assumptions=assumptions,
            idempotency_key=f"conversation-build:{conversation_id}",
            resolve_unanswered_questions=True,
        )
        plan = create_project_generation_plan(
            outcome.build,
            owner_user_id,
            generation_mode=generation_mode,
        )
        job_id = next(iter(plan.jobs))
        if plan.status.value == "planned" and not self.requires_request_bound_execution():
            self._launch(plan.plan_id, owner_user_id)
        return (
            ContextBuildExecution(
                build_id=outcome.build.build_id,
                plan_id=plan.plan_id,
                job_id=job_id,
                status=plan.status.value,
            ),
            outcome.workflow,
        )

    @staticmethod
    def requires_request_bound_execution() -> bool:
        """Return whether the runtime can discard work after an HTTP response."""

        return str(config.get("VERCEL") or "").strip() == "1"

    @classmethod
    async def execute(cls, plan_id: str, owner_user_id: str):
        """Keep a build attached to an HTTP invocation on serverless runtimes."""

        cancellation_event = Event()
        with cls._cancellation_lock:
            if plan_id in cls._cancellation_events:
                return get_project_generation_plan(plan_id, owner_user_id)
            cls._cancellation_events[plan_id] = cancellation_event

        try:
            return await execute_project_generation_plan(
                plan_id,
                owner_user_id,
                cancellation_check=cancellation_event.is_set,
            )
        finally:
            with cls._cancellation_lock:
                if cls._cancellation_events.get(plan_id) is cancellation_event:
                    cls._cancellation_events.pop(plan_id, None)

    @classmethod
    def _launch(cls, plan_id: str, owner_user_id: str) -> None:
        """Run independently of the HTTP response lifecycle so the UI can begin polling immediately."""

        cancellation_event = Event()
        oidc_token = current_vertex_oidc_token()
        with cls._cancellation_lock:
            cls._cancellation_events[plan_id] = cancellation_event

        def run() -> None:
            context_token = bind_vertex_oidc_token(oidc_token)
            try:
                asyncio.run(execute_project_generation_plan(
                    plan_id,
                    owner_user_id,
                    cancellation_check=cancellation_event.is_set,
                ))
            except Exception:
                logger.exception("Detached generation plan failed: plan_id=%s", plan_id)
            finally:
                reset_vertex_oidc_token(context_token)
                with cls._cancellation_lock:
                    if cls._cancellation_events.get(plan_id) is cancellation_event:
                        cls._cancellation_events.pop(plan_id, None)

        Thread(target=run, name=f"forma-build-{plan_id}", daemon=True).start()

    @classmethod
    def signal_cancel(cls, plan_id: str) -> bool:
        with cls._cancellation_lock:
            cancellation_event = cls._cancellation_events.get(plan_id)
        if cancellation_event is None:
            return False
        cancellation_event.set()
        return True


def context_build_dispatcher() -> ContextBuildDispatcher:
    return ContextBuildDispatcher()


__all__ = ["ContextBuildDispatcher", "context_build_dispatcher"]
