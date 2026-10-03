from __future__ import annotations

from opencad import Part, get_default_context


def box(length: float, width: float, height: float, center: tuple[float, float, float], name: str):
    """Create a positioned rectangular solid."""
    return Part(name=name).box(length, width, height, name=name).translate(center, name=name + " positioned")


# Current-state benchmark geometry for:
# "Design a wearable mask that can mechanically transition between covering
#  the wearer's face and revealing it."
#
# This intentionally uses only capabilities present in Forma/OpenCAD v0.3.6:
# native solid bodies + rigid revolute joints. There is no first-class human
# body/reference model, action contract, or firmware representation yet.

# Coordinate system: X left/right, Y front/back, Z vertical.
# The mask is centered around the origin and modeled at approximate adult-head scale.

# Static wearable frame.
top = box(174.0, 14.0, 12.0, (0.0, 14.0, 76.0), "Top brow frame")
left_temple = box(14.0, 76.0, 50.0, (-87.0, 35.0, 55.0), "Left temple rail")
right_temple = box(14.0, 76.0, 18.0, (87.0, 35.0, 55.0), "Right temple rail")
left_hinge_block = box(18.0, 24.0, 34.0, (-80.0, 4.0, 32.0), "Left hinge block")
right_hinge_block = box(18.0, 24.0, 34.0, (80.0, 4.0, 32.0), "Right hinge block")
controller = box(54.0, 24.0, 12.0, (0.0, 30.0, 78.0), "Controller enclosure")
left_servo = box(24.0, 28.0, 34.0, (-75.0, 14.0, 46.0), "Left servo envelope")
right_servo = box(24.0, 28.0, 34.0, (75.0, 14.0, 46.0), "Right servo envelope")

frame = top.union(left_temple, name="Frame plus left temple")
frame = frame.union(right_temple, name="Frame plus right temple")
frame = frame.union(left_hinge_block, name="Frame plus left hinge")
frame = frame.union(right_hinge_block, name="Frame plus right hinge")
frame = frame.union(controller, name="Frame plus controller")
frame = frame.union(left_servo, name="Frame plus left servo")
frame = frame.union(right_servo, name="Complete wearable frame")

# Two front panels. They meet at the centerline in the covered pose.
left_panel_upper = box(77.0, 6.0, 86.0, (-39.5, -14.0, 25.0), "Left face panel upper")
left_panel_lower = box(65.0, 6.0, 46.0, (-34.0, -13.0, -35.0), "Left face panel lower")
left_panel = left_panel_upper.union(left_panel_lower, name="Left moving mask panel")

right_panel_upper = box(77.0, 6.0, 86.0, (39.5, -14.0, 25.0), "Right face panel upper")
right_panel_lower = box(65.0, 6.0, 46.0, (34.0, -13.0, -35.0), "Right face panel lower")
right_panel = right_panel_upper.union(right_panel_lower, name="Right moving mask panel")

context = get_default_context()

# Revolute joints approximate side hinges. Open = panels swing outward.
left_joint = context.registry.call(
    "create_kinematic_joint",
    {
        "joint_id": "left-panel-hinge",
        "type": "revolute",
        "parent_shape_id": frame.shape_id,
        "child_shape_id": left_panel.shape_id,
        "axis": (0.0, 0.0, 1.0),
        "origin_mm": (-78.0, -11.0, 28.0),
        "lower_limit": 0.0,
        "upper_limit": 1.3962634015954636,
        "label": "Left panel reveal",
        "metadata": {
            "forma_target_ref": "MASK_LEFT",
            "notes": "Current v0.3.6 rigid-joint approximation of reveal_face.",
        },
    },
)
if not left_joint.ok:
    raise RuntimeError(left_joint.message)

right_joint = context.registry.call(
    "create_kinematic_joint",
    {
        "joint_id": "right-panel-hinge",
        "type": "revolute",
        "parent_shape_id": frame.shape_id,
        "child_shape_id": right_panel.shape_id,
        "axis": (0.0, 0.0, 1.0),
        "origin_mm": (78.0, -11.0, 28.0),
        "lower_limit": -1.3962634015954636,
        "upper_limit": 0.0,
        "label": "Right panel reveal",
        "metadata": {
            "forma_target_ref": "MASK_RIGHT",
            "notes": "Current v0.3.6 rigid-joint approximation of reveal_face.",
        },
    },
)
if not right_joint.ok:
    raise RuntimeError(right_joint.message)

# Export all three physical bodies to the native STEP.
FORMA_EXPORT_SHAPE_IDS = [frame.shape_id, left_panel.shape_id, right_panel.shape_id]
FORMA_PREVIEW_BODIES = [
    {"shape_id": frame.shape_id, "target_ref": "MASK_FRAME", "name": "Wearable frame"},
    {"shape_id": left_panel.shape_id, "target_ref": "MASK_LEFT", "name": "Left moving panel"},
    {"shape_id": right_panel.shape_id, "target_ref": "MASK_RIGHT", "name": "Right moving panel"},
]

# Leave a concrete final shape in the runtime as well.
model = frame
