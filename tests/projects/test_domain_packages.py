from __future__ import annotations

from pathlib import Path
import unittest

from forma_core.signups.models import AlphaSignupRequest
from forma_core.workspaces.chats.models import (
    Chat,
    ChatMessage,
    ChatUpsertRequest,
    ProjectChat,
    ProjectChatUpsertRequest,
)
from forma_core.workspaces.models import Workspace
from forma_core.workspaces.projects.iteration import ProjectIterator
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, Project
from forma_core.workspaces.projects.objects import build_project_object


class DomainPackageTests(unittest.TestCase):
    def test_project_and_signup_models_have_canonical_domain_modules(self) -> None:
        self.assertEqual("forma_core.workspaces.projects.models", HardwareIntermediateRepresentation.__module__)
        self.assertEqual("forma_core.workspaces.projects.iteration", ProjectIterator.__module__)
        self.assertEqual("forma_core.workspaces.projects.objects", build_project_object.__module__)
        self.assertEqual("forma_core.signups.models", AlphaSignupRequest.__module__)

    def test_legacy_domain_modules_are_removed(self) -> None:
        package_root = Path(__file__).resolve().parents[2] / "forma_core"
        legacy_modules = ("iteration.py", "models.py", "project_objects.py")
        self.assertEqual([], [name for name in legacy_modules if (package_root / name).exists()])


class ChatModelTests(unittest.TestCase):
    def test_project_chat_name_remains_an_alias_for_workspace_chat(self) -> None:
        self.assertIs(ProjectChat, Chat)
        self.assertIs(ProjectChatUpsertRequest, ChatUpsertRequest)

    def test_chat_messages_preserve_pipeline_and_forward_compatible_fields(self) -> None:
        message = ChatMessage.model_validate(
            {
                "id": "message-1",
                "role": "assistant",
                "content": "Your project is ready.",
                "status": "success",
                "timestamp": "2026-07-29T12:00:00Z",
                "projectId": "project-1",
                "pipelineProgress": {"currentStepIndex": 3},
                "toolCalls": [{"name": "generate_project"}],
            }
        )
        chat = ProjectChat(
            chat_id="chat-1",
            title="Fixture design",
            messages=[message],
            created_at="2026-07-29T11:00:00Z",
            updated_at="2026-07-29T12:00:00Z",
        )

        payload = chat.model_dump(mode="json")

        self.assertEqual("project-1", payload["messages"][0]["projectId"])
        self.assertEqual(3, payload["messages"][0]["pipelineProgress"]["currentStepIndex"])
        self.assertEqual("generate_project", payload["messages"][0]["toolCalls"][0]["name"])


class WorkspaceModelTests(unittest.TestCase):
    @staticmethod
    def _chat(chat_id: str) -> Chat:
        return Chat(
            chat_id=chat_id,
            title=f"Chat {chat_id}",
            created_at="2026-07-29T11:00:00Z",
            updated_at="2026-07-29T12:00:00Z",
        )

    @staticmethod
    def _project(project_id: str, chat_id: str | None) -> Project:
        return Project(
            project_id=project_id,
            chat_id=chat_id,
            title=f"Project {project_id}",
            prompt="Build a fixture.",
            hardware_ir={},
            created_at="2026-07-29T12:00:00Z",
        )

    def test_workspace_groups_projects_into_threads_without_owning_their_lifecycle(self) -> None:
        workspace = Workspace(
            owner_user_id="user-1",
            chats=[self._chat("chat-1"), self._chat("chat-2")],
            projects=[
                self._project("thread-project", "chat-1"),
                self._project("standalone-project", None),
                self._project("orphaned-project", "deleted-chat"),
            ],
        )

        threads = workspace.design_threads()

        self.assertEqual(["chat-1", "chat-2"], [thread.chat.chat_id for thread in threads])
        self.assertEqual(["thread-project"], [project.project_id for project in threads[0].projects])
        self.assertEqual([], threads[1].projects)
        self.assertEqual(
            ["standalone-project", "orphaned-project"],
            [project.project_id for project in workspace.standalone_projects()],
        )


if __name__ == "__main__":
    unittest.main()
