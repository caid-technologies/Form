"""Render a synchronized CAD pick/place scene and a rigidly mounted camera POV.

The RGB threshold detector selects a red part from three known fixture slots.
The remainder is a scripted, analytic-IK animation, not hardware control.
Run with Python + cadquery, numpy, scipy, pyvista (EGL-capable VTK), and Pillow:
  python vision_scene.py --servo-step reused-servo.step --output vision-output
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import cadquery as cq
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pyvista as pv
from scipy.ndimage import label

from build import box, cyl, frames, make_parts, rot, tr, transformed


WIDTH, HEIGHT = 880, 660
FPS = 15
SOURCE_YAW, DEST_YAW = -30.0, 35.0
RADIUS, PICK_Z = 110.0, 19.0
TCP = np.array([34.0, 0.0, 4.0, 1.0])
OPEN_JAW = 28.0
SLOT_OFFSETS = (-34.0, 0.0, 34.0)
CAM_DIRECTION = np.array([38.0, 0.0, -24.0])
CAM_DIRECTION /= np.linalg.norm(CAM_DIRECTION)
CAM_ORIGIN = np.array([-4.0, 0.0, 28.0])
CAM_UP = np.array([-CAM_DIRECTION[2], 0.0, CAM_DIRECTION[0]])
CAM_FRAME = np.eye(4)
CAM_FRAME[:3, :3] = np.column_stack(([0.0, 1.0, 0.0], CAM_UP, CAM_DIRECTION))
CAM_FRAME[:3, 3] = CAM_ORIGIN
STAGES = [
    ("scan", "01 / FIND RED", 24),
    ("approach", "02 / APPROACH", 20),
    ("descend", "03 / ALIGN", 12),
    ("grip", "04 / GRIP", 12),
    ("lift", "05 / LIFT", 18),
    ("transfer", "06 / TRANSFER", 28),
    ("lower", "07 / PLACE", 18),
    ("release", "08 / RELEASE", 12),
    ("retract", "09 / RETRACT", 18),
    ("done", "10 / SORT COMPLETE", 18),
]


def inverse_kinematics(radius, height, yaw, jaw):
    """Exact 2-link IK to TCP; the fixed gripper extends the second link."""
    length = math.hypot(45 + TCP[0], TCP[2])
    offset = math.atan2(TCP[2], 45 + TCP[0])
    dz = height - 65
    cosine = (radius**2 + dz**2 - 55**2 - length**2) / (2 * 55 * length)
    if not -1 <= cosine <= 1:
        raise ValueError(f"Unreachable TCP: {radius=}, {height=}")
    beta = -math.acos(cosine)
    alpha = math.atan2(dz, radius) - math.atan2(length * math.sin(beta), 55 + length * math.cos(beta))
    angles = [float(yaw), math.degrees(alpha), math.degrees(beta - offset), float(jaw)]
    pose = frames(*angles)
    expected = (rot("z", yaw) @ np.array([radius, 0.0, height, 1.0]))[:3]
    assert np.linalg.norm((pose["wrist"] @ TCP)[:3] - expected) < 1e-8
    return pose, angles


def camera_arm(servo_shape):
    remove = {"fixed-jaw", "moving-jaw", "fixed-pad", "moving-pad"}
    parts = [p for p in make_parts(servo_shape) if p["name"] not in remove]

    def add(name, shape, color, group="wrist", local=None, printed=False):
        parts.append(dict(name=name, shape=shape, color=color, group=group,
                          local=np.eye(4) if local is None else local, printed=printed))

    # Side pinch: 11 mm between the pad faces at zero jaw angle.
    fixed = box(34, 3, 6, (19, -11, 4)).fuse(box(8, 5, 8, (34, -10, 4)))
    moving = box(29, 3, 6, (14.5, 10, 0)).fuse(
        box(6, 11, 6, (2, 5, 0)), box(8, 5, 8, (29, 10, 0)))
    add("vision-fixed-jaw", fixed, "#dbe3e9", printed=True)
    add("vision-moving-jaw", moving, "#dbe3e9", "jaw", printed=True)
    add("vision-fixed-pad", box(8, 2, 8, (34, -6.5, 4)), "#f4ac65")
    add("vision-moving-pad", box(8, 2, 8, (29, 6.5, 0)), "#f4ac65", "jaw")
    # Generic miniature board camera; no specific electronics vendor is implied.
    mast = box(3, 22, 3, (-9, 0, -6)).fuse(
        box(3, 3, 29, (-9, -8, 7.5)), box(3, 3, 29, (-9, 8, 7.5)),
        box(3, 22, 3, (-9, 0, 22)))
    add("camera-mount", mast, "#0ab9b0", printed=True)
    board = box(20, 20, 1.2, (0, 0, -5.2))
    for x in (-8, 8):
        for y in (-8, 8):
            board = board.cut(cyl(.8, 4, (x, y, -7)))
    add("camera-board", board, "#14795b", local=CAM_FRAME)
    add("camera-body", box(15, 15, 5, (0, 0, -2)), "#203b48", local=CAM_FRAME)
    add("camera-lens-barrel", cyl(4.5, 5), "#101923", local=CAM_FRAME)
    add("camera-optical-glass", cyl(3.3, .3, (0, 0, 5)), "#60e8ed", local=CAM_FRAME)
    add("camera-status-led", cyl(.9, .3, (6, -6, .5)), "#8bffd3", local=CAM_FRAME)
    return parts


def polydata(shape):
    vertices, triangles = shape.tessellate(.16, .22)
    return pv.PolyData(np.array([[v.x, v.y, v.z] for v in vertices]),
                       np.array([[3, *t] for t in triangles]).flatten())


def fixture(yaw):
    return rot("z", yaw) @ tr(RADIUS, 0, 0)


def make_scene():
    scene = []

    def add(name, shape, color, matrix=None):
        scene.append(dict(name=name, shape=shape, color=color,
                          matrix=np.eye(4) if matrix is None else matrix))

    add("work-cell", box(300, 270, 5, (53, 0, -3.5)), "#dae4eb")
    add("arm-plinth", box(104, 92, 3, (0, 0, -1.5)), "#27495d")
    for name, yaw in (("source", SOURCE_YAW), ("destination", DEST_YAW)):
        m = fixture(yaw)
        width = 100 if name == "source" else 76
        add(name + "-deck", box(32, width, 4, (0, 0, 10)), "#f5f8fa", m)
        # Thin supports leave clearance below the overhanging deck.
        for yy in (-width / 2 + 10, width / 2 - 10):
            add(name + f"-foot-{yy}", box(16, 5, 8, (0, yy, 4)), "#647d8d", m)
        if name == "destination":
            add("destination-outline", box(22, 22, .3, (0, 0, 12.15)).cut(
                box(20, 20, 2, (0, 0, 12))), "#06a798", m)
            for yy in (-36, 36):
                add(f"tray-rim-{yy}", box(32, 2, 3, (0, yy, 13.5)), "#0cae9f", m)
        else:
            for yy in SLOT_OFFSETS:
                outline = box(16, 16, .2, (0, yy, 12.1)).cut(box(14, 14, 2, (0, yy, 12)))
                add(f"source-slot-{yy}", outline, "#b9cbd6", m)
    # The red workpiece is the only moving scene object.
    cube = box(11, 11, 14)
    add("blue-distractor", cube, "#2e83d3", fixture(SOURCE_YAW) @ tr(0, SLOT_OFFSETS[0], PICK_Z))
    add("yellow-distractor", cube, "#f6c443", fixture(SOURCE_YAW) @ tr(0, SLOT_OFFSETS[2], PICK_Z))
    for text, pos, size in (("PICK", (83, -109, -.9), 8), ("SORT", (87, 86, -.9), 8),
                            ("VISION CELL  /  01", (-22, 103, -.9), 5)):
        add("label-" + text, cq.Workplane("XY").text(text, size, .15, font="DejaVu Sans", combine=True).val().translate(pos), "#547283")
    return scene, cube


def configure_plotter(pov=False):
    p = pv.Plotter(off_screen=True, window_size=(WIDTH, HEIGHT), lighting="none")
    p.set_background("#eaf0f4")
    p.add_mesh(pv.Plane(center=(40, 0, -6.1), i_size=2500, j_size=2500), color="#eaf0f4")
    p.add_light(pv.Light(position=(100, -200, 400), focal_point=(60, 0, 30), intensity=.85))
    p.add_light(pv.Light(position=(-250, 100, 180), focal_point=(60, 0, 30), intensity=.35))
    if pov:
        p.camera.view_angle = 68
        p.camera.clipping_range = (.7, 1500)
    else:
        p.camera_position = [(315, -390, 320), (48, 0, 43), (0, 0, 1)]
        p.camera.parallel_projection = True
        p.camera.parallel_scale = 131
    p.enable_anti_aliasing("ssaa")
    return p


def camera_pose(plotter, wrist):
    center = CAM_ORIGIN + CAM_DIRECTION * 5.6
    eye = (wrist @ np.r_[center, 1])[:3]
    look = wrist[:3, :3] @ CAM_DIRECTION
    up = wrist[:3, :3] @ CAM_UP
    plotter.camera_position = [eye.tolist(), (eye + 100 * look).tolist(), up.tolist()]
    plotter.camera.clipping_range = (.7, 1500)
    return dict(position_mm=eye.tolist(), direction=look.tolist(), up=up.tolist(), vertical_fov_deg=68)


def detect_red(rgb):
    """Actual per-frame RGB segmentation; never draw an inferred hidden box."""
    pixels = rgb.astype(float)
    r, g, b = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    mask = (r > 65) & (r > 1.7 * g) & (r > 1.3 * b) & (g < 130)
    components, count = label(mask)
    if not count:
        return None
    sizes = np.bincount(components.ravel()); sizes[0] = 0
    component = sizes.argmax()
    if sizes[component] < 30:
        return None
    yy, xx = np.where(components == component)
    return dict(bbox=[int(xx.min()), int(yy.min()), int(xx.max()), int(yy.max())],
                centroid=[float(xx.mean()), float(yy.mean())], pixels=int(sizes[component]))


def project(plotter, point):
    plotter.renderer.SetWorldPoint(*point, 1)
    plotter.renderer.WorldToDisplay()
    x, y, z = plotter.renderer.GetDisplayPoint()
    return [float(x), float(HEIGHT - 1 - y)]


def sequence(source_yaw=SOURCE_YAW, source_radius=RADIUS):
    """No hidden reset: finish with the part visibly in the destination tray."""
    for stage, caption, count in STAGES:
        for k in range(count):
            t = (k + 1) / count
            s = t * t * (3 - 2 * t)
            yaw, radius, z, jaw, held = source_yaw, source_radius, 65, OPEN_JAW, False
            if stage == "approach": z = 65 - 31 * s
            elif stage == "descend": z = 34 + (PICK_Z - 34) * s
            elif stage == "grip": z, jaw = PICK_Z, OPEN_JAW * (1 - s)
            elif stage == "lift": z, jaw, held = PICK_Z + (67 - PICK_Z) * s, 0, True
            elif stage == "transfer":
                yaw, z, jaw, held = source_yaw + (DEST_YAW - source_yaw) * s, 67 + 9 * math.sin(math.pi * s), 0, True
                radius = source_radius + (RADIUS - source_radius) * s - 7 * math.sin(math.pi * s)
            elif stage == "lower": yaw, radius, z, jaw, held = DEST_YAW, RADIUS, 67 + (PICK_Z - 67) * s, 0, True
            elif stage == "release": yaw, radius, z, jaw = DEST_YAW, RADIUS, PICK_Z, OPEN_JAW * s
            elif stage in ("retract", "done"):
                yaw, radius, z = DEST_YAW, RADIUS, PICK_Z + (65 - PICK_Z) * s if stage == "retract" else 65
            yield dict(stage=stage, caption=caption, yaw=yaw, radius=radius, z=z, jaw=jaw, held=held)


FONT_PATH = "/usr/share/fonts/truetype/dejavu/"
FONTS = {"title": ImageFont.truetype(FONT_PATH + "DejaVuSans-Bold.ttf", 26),
         "body": ImageFont.truetype(FONT_PATH + "DejaVuSans.ttf", 16),
         "small": ImageFont.truetype(FONT_PATH + "DejaVuSans.ttf", 13),
         "mono": ImageFont.truetype(FONT_PATH + "DejaVuSansMono.ttf", 14)}


def annotate(rgb, entry, index, total, detection=None, pov=False, camera_pixel=None):
    im = Image.fromarray(rgb)
    draw = ImageDraw.Draw(im)
    if pov and detection is not None:
        x0, y0, x1, y1 = detection["bbox"]
        x0, y0, x1, y1 = x0 - 7, y0 - 7, x1 + 7, y1 + 7
        color = "#83ffe5"
        length = min(18, (x1 - x0) // 3)
        for x, sx in ((x0, 1), (x1, -1)):
            for y, sy in ((y0, 1), (y1, -1)):
                draw.line([(x, y + sy * length), (x, y), (x + sx * length, y)], fill=color, width=3)
        cx, cy = detection["centroid"]
        draw.line((cx - 6, cy, cx + 6, cy), fill=color, width=1)
        draw.line((cx, cy - 6, cx, cy + 6), fill=color, width=1)
        ly = max(96, y0 - 27)
        draw.rectangle((max(5, x0), ly, max(5, x0) + 139, ly + 22), fill="#103b3e")
        draw.text((max(5, x0) + 7, ly + 3), "RED / DETECTED", font=FONTS["small"], fill=color)
    if not pov and camera_pixel and entry["stage"] == "scan":
        x, y = camera_pixel
        lx, ly = 56, 154
        draw.line([(x, y), (x - 40, ly + 26), (lx + 8, ly + 26)], fill="#168f93", width=2)
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#00bcb9")
        draw.text((lx + 8, ly - 1), "WRIST CAMERA", font=FONTS["small"], fill="#126d77")
    draw.rectangle((0, 0, WIDTH, 86), fill="#112b3a")
    title = "WRIST CAMERA / RGB" if pov else "SEE. PICK. SORT."
    draw.text((27, 14), title, font=FONTS["title"], fill="#f4f9fc")
    subtitle = "Rigid camera mount · 68° field of view" if pov else "Four SG90 servos + a gripper-mounted camera"
    draw.text((29, 52), subtitle, font=FONTS["body"], fill="#a8c9d9")
    draw.text((WIDTH - 163, 21), "SIMULATION", font=FONTS["mono"], fill="#74ddd3")
    draw.rectangle((0, HEIGHT - 75, WIDTH, HEIGHT), fill="#112b3a")
    draw.text((27, HEIGHT - 61), entry["caption"], font=FONTS["body"], fill="#ffffff")
    if pov:
        status = "RGB mask: red visible" if detection else "Red part outside view / occluded"
    else:
        status = "Select the red part. Leave blue and yellow in place."
        if entry["stage"] in ("release", "retract", "done"):
            status = "Red part placed. Blue and yellow remain in the pick zone."
    draw.text((27, HEIGHT - 34), status, font=FONTS["small"], fill="#a8c9d9")
    timecode = f"{index / FPS:04.1f}s / {total / FPS:.1f}s"
    draw.text((WIDTH - 167, HEIGHT - 59), timecode, font=FONTS["mono"], fill="#a8c9d9")
    draw.rectangle((0, HEIGHT - 4, int(WIDTH * (index + 1) / total), HEIGHT), fill="#38d8bf")
    return im


def save_gif(images, path):
    # One palette for the whole clip avoids flickering hues and reduces size.
    samples = images[::max(1, len(images) // 12)]
    swatch = Image.new("RGB", (220 * len(samples), 165))
    for i, im in enumerate(samples): swatch.paste(im.resize((220, 165)), (220 * i, 0))
    palette = swatch.quantize(colors=256, method=Image.Quantize.MAXCOVERAGE)
    indexed = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    # GIF durations are in 10 ms units; this repeating pattern gives exactly 15 fps.
    durations = [70 if i % 3 != 2 else 60 for i in range(len(images))]
    pending = path.with_suffix(".rendering")
    indexed[0].save(pending, format="GIF", save_all=True, append_images=indexed[1:], duration=durations,
                    loop=0, optimize=False, disposal=2)
    with Image.open(pending) as check:
        total_ms = 0
        for i in range(check.n_frames):
            check.seek(i); check.load(); total_ms += check.info["duration"]
        if total_ms != sum(durations): raise RuntimeError(f"Incomplete GIF: {pending}")
    pending.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--servo-step", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True)
    parts = camera_arm(cq.importers.importStep(str(args.servo_step)).val())
    scene, cube = make_scene()
    outside, pov = configure_plotter(), configure_plotter(True)
    plotters = [outside, pov]
    arm_actors = [[], []]
    for item in parts:
        mesh = polydata(item["shape"])
        for p, actors in zip(plotters, arm_actors):
            actor = p.add_mesh(mesh, color=item["color"], smooth_shading=True, ambient=.3,
                               diffuse=.7, specular=.25, specular_power=24)
            actors.append((actor, item))
    for item in scene:
        mesh = polydata(item["shape"])
        for p in plotters:
            actor = p.add_mesh(mesh, color=item["color"], smooth_shading=False, ambient=.4, diffuse=.6)
            actor.user_matrix = item["matrix"]
    workpieces = [p.add_mesh(polydata(cube), color="#ef4655", ambient=.6, diffuse=.4,
                             smooth_shading=False) for p in plotters]
    initial_object = fixture(SOURCE_YAW) @ tr(0, 0, PICK_Z)
    final_object = fixture(DEST_YAW) @ tr(0, 0, PICK_Z)
    pick_pose, _ = inverse_kinematics(RADIUS, PICK_Z, SOURCE_YAW, 0)
    object_in_wrist = np.linalg.inv(pick_pose["wrist"]) @ initial_object
    # Observe before planning: the measured image selects the calibrated slot
    # whose position supplies the source radius and yaw to the IK trajectory.
    scan_pose, _ = inverse_kinematics(RADIUS, 65, SOURCE_YAW, OPEN_JAW)
    for actor, item in arm_actors[1]: actor.user_matrix = scan_pose[item["group"]] @ item["local"]
    workpieces[1].user_matrix = initial_object
    camera_pose(pov, scan_pose["wrist"])
    pov.render()
    scan_detection = detect_red(pov.screenshot(return_img=True)[:, :, :3])
    if scan_detection is None: raise RuntimeError("Initial camera frame did not detect the red part")
    slots = [(fixture(SOURCE_YAW) @ np.array([0, y, PICK_Z, 1]))[:3] for y in SLOT_OFFSETS]
    pixels = [project(pov, point) for point in slots]
    distances = [float(np.linalg.norm(np.array(p) - scan_detection["centroid"])) for p in pixels]
    selected = int(np.argmin(distances))
    if distances[selected] > 50: raise RuntimeError("The RGB detection does not match a calibrated fixture slot")
    source_center = slots[selected]
    source_yaw = math.degrees(math.atan2(source_center[1], source_center[0]))
    source_radius = float(np.hypot(*source_center[:2]))
    timeline = list(sequence(source_yaw, source_radius))
    histories, external_images, pov_images, combined_images = [], [], [], []
    fixture_selection = dict(method="RGB threshold + nearest calibrated fixture slot", detected=scan_detection,
                             candidate_pixels=pixels, pixel_distances=distances, selected_slot=selected,
                             selected_world_center_mm=source_center.tolist(),
                             planned_source_yaw_deg=source_yaw, planned_source_radius_mm=source_radius)
    preview_indices = [0, 55, 67, 83, 101, 131, 149, 179]
    if args.preview:
        iterable = [(i, timeline[i]) for i in preview_indices]
    else:
        iterable = list(enumerate(timeline))
    for index, entry in iterable:
        pose, angles = inverse_kinematics(entry["radius"], entry["z"], entry["yaw"], entry["jaw"])
        for actors in arm_actors:
            for actor, item in actors: actor.user_matrix = pose[item["group"]] @ item["local"]
        if entry["held"]:
            object_matrix = pose["wrist"] @ object_in_wrist
        elif entry["stage"] in ("release", "retract", "done"):
            object_matrix = final_object
        else:
            object_matrix = initial_object
        for actor in workpieces: actor.user_matrix = object_matrix
        cam = camera_pose(pov, pose["wrist"])
        for p in plotters: p.render()
        external_rgb, pov_rgb = [p.screenshot(return_img=True)[:, :, :3] for p in plotters]
        detection = detect_red(pov_rgb)
        ext = annotate(external_rgb, entry, index, len(timeline), camera_pixel=project(outside, cam["position_mm"]))
        eye = annotate(pov_rgb, entry, index, len(timeline), detection=detection, pov=True)
        external_images.append(ext); pov_images.append(eye)
        pair = Image.new("RGB", (1320, 495))
        pair.paste(ext.resize((660, 495), Image.Resampling.LANCZOS), (0, 0))
        pair.paste(eye.resize((660, 495), Image.Resampling.LANCZOS), (660, 0))
        combined_images.append(pair)
        if index in preview_indices or index == len(timeline) - 1:
            ext.save(out / f"external-{index:03d}.png")
            eye.save(out / f"pov-{index:03d}.png")
            pair.save(out / f"paired-{index:03d}.png")
        histories.append(dict(frame=index, time_s=index / FPS, **entry, joint_angles_deg=angles,
                              camera=cam, rgb_detection=detection, object_transform=object_matrix.tolist(),
                              tcp_world_mm=(pose["wrist"] @ TCP)[:3].tolist()))
        if index % 30 == 0: print(f"Rendered {index + 1}/{len(timeline)}: {entry['stage']}", flush=True)
    if not args.preview:
        save_gif(external_images, out / "sg90-vision-pick-and-place.gif")
        save_gif(pov_images, out / "sg90-wrist-camera-pov.gif")
        save_gif(combined_images, out / "sg90-vision-synchronized.gif")
        assembly = cq.Assembly(name="SG90 arm with wrist camera")
        ready_pose, _ = inverse_kinematics(RADIUS, 65, SOURCE_YAW, OPEN_JAW)
        for item in parts:
            assembly.add(transformed(item["shape"], ready_pose[item["group"]] @ item["local"]),
                         name=item["name"], color=cq.Color(item["color"]))
        assembly.save(str(out / "sg90-camera-arm.step"))
        for item in scene:
            assembly.add(transformed(item["shape"], item["matrix"]), name=item["name"], color=cq.Color(item["color"]))
        assembly.add(transformed(cube, initial_object), name="red-workpiece", color=cq.Color("#ef4655"))
        assembly.save(str(out / "sg90-vision-cell.step"))
        printed = out / "camera-and-jaws-stl"; printed.mkdir(exist_ok=True)
        for item in parts:
            if item["name"] in ("camera-mount", "vision-fixed-jaw", "vision-moving-jaw"):
                cq.exporters.export(item["shape"], str(printed / (item["name"] + ".stl")), tolerance=.08)
    evidence = dict(rendered_frames=len(histories), fps=FPS, timeline_frames=len(timeline),
                    simulation=True, physical_tested=False, camera_mount="rigid wrist transform",
                    servo_reference=str(args.servo_step), fixture_selection=fixture_selection,
                    limitations=["Generic camera envelope; exact hardware, lens, mount, wiring, torque and collisions are not certified.",
                                 "Detector sees rendered RGB. IK motion is scripted through known fixture positions; no real robot or camera is connected.",
                                 "The changed fingers are a concept for an 11 x 11 x 14 mm sample; verify linkage, clearance and fit before fabrication."],
                    frames=histories)
    (out / ("preview-evidence.json" if args.preview else "vision-scene-evidence.json")).write_text(json.dumps(evidence, indent=2))
    for p in plotters: p.close()
    print(json.dumps(dict(output=str(out), frames=len(histories), vision=fixture_selection)), flush=True)


if __name__ == "__main__":
    main()
