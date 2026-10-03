from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """One persisted message in a project chat.

    Unknown fields are retained so older clients and pipeline metadata remain
    round-trippable while the message contract becomes explicitly typed.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    role: str | None = None
    content: str | None = None
    status: str | None = None
    timestamp: str | None = None
    projectId: str | None = None
    revisionId: str | None = None
    pipelineProgress: Dict[str, Any] | None = None


class Chat(BaseModel):
    """Conversation timeline contained by a workspace."""

    chat_id: str
    title: str
    messages: List[ChatMessage] = Field(default_factory=list)
    created_at: str
    updated_at: str


ProjectChat = Chat


class ChatUpsertRequest(BaseModel):
    """Payload accepted when creating or updating a project chat."""

    chat_id: str | None = None
    title: str | None = None
    messages: List[ChatMessage] | None = None


ProjectChatUpsertRequest = ChatUpsertRequest
