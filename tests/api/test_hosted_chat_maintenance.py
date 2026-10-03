from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from apps.api import a2a, main
from apps.api.a2a import A2AMessage, handle_mcp_json_rpc, submit_a2a_message
from apps.api.auth import UserContext
from apps.api.hosted_chat import HOSTED_CHAT_UNAVAILABLE_CODE, require_hosted_chat_enabled
from forma_core.config.runtime import (
    HOSTED_CHAT_UNAVAILABLE_MESSAGE,
    HostedChatUnavailableError,
)
from forma_core.workspaces.projects.models import (
    ClarifyingQuestionsRequest,
    IterateProjectRequest,
    ProjectUpdateRequest,
    VideoSelfCorrectRequest,
)


def _disabled_hosted_chat_environment() -> dict[str, str]:
    return {
        "FORMA_DEPLOYMENT_MODE": "hosted",
        "FORMA_DEVELOPMENT_MODE": "false",
        "FORMA_HOSTED_CHAT_ENABLED": "false",
    }


def _user() -> UserContext:
    return UserContext(
        provider="test",
        subject="maintenance-user",
        owner_user_id="maintenance-user",
        is_authenticated=True,
        is_admin=False,
    )


class HostedChatMaintenanceApiTests(unittest.IsolatedAsyncioTestCase):
    def test_http_guard_returns_a_stable_503_error_contract(self) -> None:
        with patch.dict(os.environ, _disabled_hosted_chat_environment(), clear=True):
            with self.assertRaises(HTTPException) as raised:
                require_hosted_chat_enabled()

        self.assertEqual(503, raised.exception.status_code)
        self.assertEqual(
            {
                "code": HOSTED_CHAT_UNAVAILABLE_CODE,
                "message": HOSTED_CHAT_UNAVAILABLE_MESSAGE,
            },
            raised.exception.detail,
        )

    def test_hosted_generation_routes_reject_before_backend_work(self) -> None:
        user = _user()
        with patch.dict(os.environ, _disabled_hosted_chat_environment(), clear=True):
            guarded_calls = [
                lambda: main.clarifying_questions_endpoint(ClarifyingQuestionsRequest(prompt="Build a sensor")),
                lambda: main.create_image_to_video_endpoint(
                    main.VideoImageToVideoRequest(projectId="project", image="image", prompt="animate"),
                    user,
                ),
                lambda: main.create_video_to_video_endpoint(
                    main.VideoToVideoRequest(projectId="project", video="video", prompt="animate"),
                    user,
                ),
                lambda: main.generate_project_video_prompt_endpoint("project", user),
                lambda: main.video_self_correct_project_endpoint(
                    "project",
                    VideoSelfCorrectRequest(video_url="https://example.test/video.mp4"),
                    user,
                ),
                lambda: main.update_project_endpoint(
                    "project",
                    ProjectUpdateRequest(title="Renamed project"),
                    user,
                ),
                lambda: main.remix_project_endpoint("project", user),
                lambda: main.delete_project_endpoint("project", user),
                lambda: main.restore_project_endpoint("project", user),
                lambda: main.upsert_chat_endpoint(
                    "chat",
                    main.ProjectChatUpsertRequest(title="Chat", messages=[]),
                    user,
                ),
                lambda: main.delete_chat_endpoint("chat", user),
                lambda: main.iterate_project_endpoint(
                    "project",
                    IterateProjectRequest(instruction="Change the enclosure"),
                    user,
                ),
            ]

            for guarded_call in guarded_calls:
                with self.subTest(call=guarded_call):
                    with self.assertRaises(HTTPException) as raised:
                        guarded_call()
                    self.assertEqual(503, raised.exception.status_code)

    async def test_a2a_generation_is_rejected_before_a_job_is_created(self) -> None:
        message = A2AMessage(
            sender="maintenance-agent",
            action="forma.generate_project",
            payload={"prompt": "Build a sensor"},
        )
        register = AsyncMock()
        with patch.dict(os.environ, _disabled_hosted_chat_environment(), clear=True), patch.object(
            a2a.A2A_HUB,
            "register",
            new=register,
        ), patch.object(a2a.JOB_STORE, "create_job") as create_job:
            with self.assertRaises(HostedChatUnavailableError):
                await submit_a2a_message(message, _user())

        register.assert_not_awaited()
        create_job.assert_not_called()

    async def test_mcp_generation_reports_maintenance_without_hiding_the_compiler(self) -> None:
        with patch.dict(os.environ, _disabled_hosted_chat_environment(), clear=True), patch.object(
            a2a,
            "_apply_owner_user_integrations",
        ):
            generation_response = await handle_mcp_json_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": "generation",
                    "method": "tools/call",
                    "params": {
                        "name": "forma.generate_project",
                        "arguments": {"prompt": "Build a sensor"},
                    },
                },
                _user(),
            )

            with patch.object(a2a, "get_project_identity", return_value=None), patch.object(
                a2a,
                "persist_chat_project_revision",
            ) as persist_revision:
                compile_response = await handle_mcp_json_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": "compile",
                        "method": "tools/call",
                        "params": {
                            "name": "forma.compile_project",
                            "arguments": {"project_ir": {"components": [], "nets": []}},
                        },
                    },
                    _user(),
                )

        self.assertEqual(-32004, generation_response["error"]["code"])
        self.assertEqual(HOSTED_CHAT_UNAVAILABLE_MESSAGE, generation_response["error"]["message"])
        self.assertEqual("hosted_chat_unavailable", generation_response["error"]["data"]["code"])
        self.assertTrue(compile_response["result"]["structuredContent"]["persisted"])
        persist_revision.assert_called_once()


if __name__ == "__main__":
    unittest.main()
