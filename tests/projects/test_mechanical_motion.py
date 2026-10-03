import pytest
from pydantic import ValidationError

from forma_core.workspaces.projects.models import MechanicalNotes


def _notes(**overrides):
    payload = {
        "enclosure_type": "3D Printed",
        "mounting_guidance": "Mount the mechanism to the base.",
        "manufacturability_rating": "Moderate",
        **overrides,
    }
    return MechanicalNotes.model_validate(payload)


def test_mechanical_notes_accept_revolute_and_prismatic_motion_intents():
    notes = _notes(
        motion_intents=[
            {
                "motion_id": "lid",
                "type": "revolute",
                "target_ref": "LID",
                "axis": "z",
                "pivot_mm": [-40, 10, 0],
                "min_deg": 0,
                "max_deg": 105,
            },
            {
                "motion_id": "slide",
                "type": "prismatic",
                "target_ref": "SLIDE",
                "axis": "X",
                "min_mm": -2,
                "max_mm": 8,
            },
        ]
    )

    assert [motion.type for motion in notes.motion_intents] == ["revolute", "prismatic"]
    assert notes.motion_intents[0].axis == "Z"
    assert notes.motion_intents[0].pivot_mm == [-40.0, 10.0, 0.0]
    assert notes.motion_intents[1].max_mm == 8


def test_mechanical_motion_intent_rejects_unknown_axes():
    with pytest.raises(ValidationError):
        _notes(
            motion_intents=[
                {
                    "type": "revolute",
                    "target_ref": "LID",
                    "axis": "Q",
                    "max_deg": 90,
                }
            ]
        )
