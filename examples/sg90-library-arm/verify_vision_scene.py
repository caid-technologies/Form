"""Check exported animations and sampled CAD clearance, not hardware safety.

python verify_vision_scene.py --output /path/to/vision-output --servo-step reused-servo.step
"""
import argparse
import json
from pathlib import Path

import cadquery as cq
import numpy as np
from PIL import Image

import vision_scene as v


def bbox_overlap(a, b):
    return all(min(getattr(a, axis + "max"), getattr(b, axis + "max")) >
               max(getattr(a, axis + "min"), getattr(b, axis + "min")) + .001
               for axis in "xyz")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--servo-step", type=Path, required=True)
    args = parser.parse_args(); out = args.output
    evidence = json.loads((out / "vision-scene-evidence.json").read_text())
    animation = {}
    for name in ("sg90-vision-pick-and-place.gif", "sg90-wrist-camera-pov.gif", "sg90-vision-synchronized.gif"):
        with Image.open(out / name) as im:
            durations = []
            for i in range(im.n_frames):
                im.seek(i); im.load(); durations.append(im.info["duration"])
            animation[name] = dict(frames=im.n_frames, duration_ms=sum(durations), dimensions=list(im.size))
            assert im.n_frames == 180 and sum(durations) == 12000, animation[name]
    geometry = {}
    for name in ("sg90-camera-arm.step", "sg90-vision-cell.step"):
        shape = cq.importers.importStep(str(out / name)).val()
        geometry[name] = dict(valid=shape.isValid(), solids=len(shape.Solids()))
        assert shape.isValid() and len(shape.Solids()) > 50, geometry[name]

    parts = [p for p in v.camera_arm(cq.importers.importStep(str(args.servo_step)).val())
             if "label" not in p["name"]]
    scene, cube = v.make_scene()
    obstacles = []
    for item in scene:
        if "deck" in item["name"] or "distractor" in item["name"] or item["name"] == "work-cell":
            shape = v.transformed(item["shape"], item["matrix"])
            obstacles.append((item["name"], shape, shape.BoundingBox()))
    collisions = []
    # All approach, alignment, grip, lift, place, and release frames are checked;
    # transfer and stationary frames are sampled every fourth frame.
    samples = [f for f in evidence["frames"] if f["frame"] % 4 == 0 or f["stage"] in
               ("approach", "descend", "grip", "lift", "lower", "release")]
    for frame in samples:
        pose, _ = v.inverse_kinematics(frame["radius"], frame["z"], frame["yaw"], frame["jaw"])
        shapes = [(p["name"], v.transformed(p["shape"], pose[p["group"]] @ p["local"])) for p in parts]
        shapes.append(("red-workpiece", v.transformed(cube, np.array(frame["object_transform"]))))
        for name, shape in shapes:
            bounds = shape.BoundingBox()
            for obstacle_name, obstacle, obstacle_bounds in obstacles:
                if bbox_overlap(bounds, obstacle_bounds):
                    volume = shape.intersect(obstacle).Volume()
                    if volume > .05:
                        collisions.append(dict(frame=frame["frame"], part=name, obstacle=obstacle_name, volume_mm3=round(volume, 4)))
    matrices = [np.array(f["object_transform"]) for f in evidence["frames"]]
    initial = v.fixture(v.SOURCE_YAW) @ v.tr(0, 0, v.PICK_Z)
    final = v.fixture(v.DEST_YAW) @ v.tr(0, 0, v.PICK_Z)
    assert np.allclose(matrices[0], initial) and np.allclose(matrices[-1], final)
    # The object stays rigid relative to the gripper throughout transport.
    held_transforms = []
    for frame, matrix in zip(evidence["frames"], matrices):
        if frame["held"]:
            pose, _ = v.inverse_kinematics(frame["radius"], frame["z"], frame["yaw"], frame["jaw"])
            held_transforms.append(np.linalg.inv(pose["wrist"]) @ matrix)
    rigid_error = max(float(np.max(np.abs(m - held_transforms[0]))) for m in held_transforms)
    assert rigid_error < 1e-9
    assert np.allclose(matrices[131], matrices[132])  # Release has no pose discontinuity.
    result = dict(animations=animation, step_geometry=geometry,
                  sampled_clearance_frames=len(samples), environment_intersections=collisions,
                  grasp_transform_max_error=rigid_error, release_pose_continuous=True,
                  red_part_in_destination=True, vision_selected_slot=evidence["fixture_selection"]["selected_slot"],
                  camera_rigidly_attached=True, physical_tested=False,
                  scope="CAD against decks, distractors and work surface. Does not certify self-collision, servo travel, friction, load, wiring, mount fit or hardware behavior.")
    (out / "vision-verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    assert not collisions, "CAD environment intersection found"


if __name__ == "__main__":
    main()
