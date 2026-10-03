"""Shared identity types for every project creation channel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProjectCreationChannel(StrEnum):
    """The product surface through which a project entered Forma."""

    HOSTED = "hosted"
    CLI = "cli"


@dataclass(frozen=True)
class ProjectIdentity:
    """Common project identity shared by hosted and CLI project variants."""

    project_id: str
    owner_user_id: str | None
    title: str
    creation_channel: ProjectCreationChannel
    created_at: str
    updated_at: str | None = None


__all__ = ["ProjectCreationChannel", "ProjectIdentity"]
