from __future__ import annotations

import pytest
from pydantic import ValidationError

from forma_core.workspaces.context.models import ContextGatheringRequest
from forma_core.workspaces.projects.generation_mode import (
    GenerationMode,
    get_generation_mode,
    is_progressive_generation,
)
from forma_core.workspaces.projects.models import GenerateProjectRequest


def test_regular_is_the_default_for_generation_requests() -> None:
    request = GenerateProjectRequest(prompt="Build a motor bracket")
    assert request.generation_mode == "regular"
    assert get_generation_mode({"design_brief_id": "brief", "visual_approval_policy": "require_approval"}) == GenerationMode.REGULAR
    assert not is_progressive_generation({"design_brief_id": "brief"})


def test_progressive_mode_is_explicit_on_both_request_surfaces() -> None:
    direct = GenerateProjectRequest(prompt="Build a motor bracket", generation_mode=" Progressive ")
    context = ContextGatheringRequest(
        conversation_id="chat-1",
        text="Build a motor bracket",
        generation_mode="progressive",
    )
    assert direct.generation_mode == "progressive"
    assert context.generation_mode == "progressive"
    assert is_progressive_generation({"generation_mode": direct.generation_mode})


def test_unknown_generation_mode_is_rejected_at_the_api_contract() -> None:
    with pytest.raises(ValidationError):
        GenerateProjectRequest(prompt="Build a motor bracket", generation_mode="legacy-ish")
