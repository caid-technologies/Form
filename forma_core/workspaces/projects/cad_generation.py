"""Native CAD generation from agent-authored HardwareIntermediateRepresentation."""

from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import sys
from typing import Any
from uuid import uuid4
import zipfile

from forma_core.config import config
from forma_core.workspaces.projects.design_lifecycle import (
    RepresentationKind,
    RepresentationStatus,
    VisualApprovalPolicy,
    VisualApprovalStatus,
    bootstrap_design_lifecycle,
    load_design_lifecycle,
    record_visual_decision,
    register_cad_representation,
    representation_fingerprint,
    stable_artifact_id,
)
from forma_core.workspaces.projects.generation_mode import is_progressive_generation
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.state import ProjectArtifact


CAD_ADAPTER_RELATIVE_PATH = Path(".agents") / "skills" / "forma-hardware" / "scripts" / "cad.py"
CAD_ADAPTER_NAME = "forma-opencad"


class CadGenerationError(RuntimeError):
    """Raised when a required native CAD artifact cannot be generated."""


def _positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _project_dimensions(project: HardwareIntermediateRepresentation) -> tuple[float, float, float]:
    mechanical = project.mechanical
    render_dimensions = mechanical.render_dimensions if mechanical is not None else None
    if render_dimensions is not None:
        return (
            _positive(render_dimensions.x_mm, 80.0),
            _positive(render_dimensions.y_mm, 60.0),
            _positive(render_dimensions.z_mm, 30.0),
        )

    placements = mechanical.component_placements if mechanical is not None else []
    if placements:
        bounds = [
            (
                float(placement.position.x_mm) - abs(float(placement.size.x_mm)) / 2.0,
                float(placement.position.x_mm) + abs(float(placement.size.x_mm)) / 2.0,
                float(placement.position.y_mm) - abs(float(placement.size.y_mm)) / 2.0,
                float(placement.position.y_mm) + abs(float(placement.size.y_mm)) / 2.0,
                float(placement.position.z_mm) - abs(float(placement.size.z_mm)) / 2.0,
                float(placement.position.z_mm) + abs(float(placement.size.z_mm)) / 2.0,
            )
            for placement in placements
        ]
        return tuple(
            max(20.0, max(item[index + 1] for item in bounds) - min(item[index] for item in bounds) + 12.0)
            for index in (0, 2, 4)
        )  # type: ignore[return-value]

    return 80.0, 60.0, 30.0


def _placement_payload(project: HardwareIntermediateRepresentation) -> list[dict[str, Any]]:
    mechanical = project.mechanical
    if mechanical is None:
        return []
    return [
        {
            "ref_des": placement.ref_des,
            "label": placement.label or "",
            "category": placement.category or "",
            "x": float(placement.position.x_mm),
            "y": float(placement.position.y_mm),
            "z": float(placement.position.z_mm),
            "sx": max(1.0, abs(float(placement.size.x_mm))),
            "sy": max(1.0, abs(float(placement.size.y_mm))),
            "sz": max(1.0, abs(float(placement.size.z_mm))),
        }
        for placement in mechanical.component_placements
    ]



def _motion_intent_payload(project: HardwareIntermediateRepresentation) -> list[dict[str, Any]]:
    """Map Forma authoring intent into the rigid OpenCAD joint contract.

    Forma owns component/reference intent. OpenCAD owns joint validation and
    pose evaluation. Compliant intent is intentionally left for the future
    deformation/simulation contract and is not coerced into a rigid joint.
    """

    mechanical = project.mechanical
    if mechanical is None:
        return []

    axis_vectors = {
        "X": (1.0, 0.0, 0.0),
        "Y": (0.0, 1.0, 0.0),
        "Z": (0.0, 0.0, 1.0),
    }
    resolved: list[dict[str, Any]] = []
    for index, intent in enumerate(mechanical.motion_intents):
        if intent.type == "compliant":
            continue
        axis = axis_vectors.get(str(intent.axis).upper())
        if axis is None:
            continue
        if intent.type == "revolute":
            lower = math.radians(float(intent.min_deg or 0.0))
            upper = math.radians(float(intent.max_deg if intent.max_deg is not None else 90.0))
        else:
            lower = float(intent.min_mm or 0.0)
            upper = float(intent.max_mm if intent.max_mm is not None else 10.0)
        resolved.append({
            "motion_id": intent.motion_id or f"{intent.target_ref}-{intent.type}-{index + 1}",
            "label": intent.label or f"{intent.target_ref} {intent.type}",
            "type": intent.type,
            "target_ref": intent.target_ref,
            "parent_ref": intent.parent_ref,
            "axis": axis,
            "origin_mm": tuple(float(value) for value in intent.pivot_mm),
            "lower_limit": lower,
            "upper_limit": upper,
            "notes": intent.notes,
        })
    return resolved


def _cad_source(project: HardwareIntermediateRepresentation) -> str:
    if project.mechanical and project.mechanical.mechanism_benchmark is not None:
        from forma_core.workspaces.projects.mechanism_benchmarks import mechanism_cad_source
        return mechanism_cad_source(project.mechanical.mechanism_benchmark)
    if project.mechanical and project.mechanical.cad_operations:
        from forma_core.workspaces.projects.solid_cad import solid_cad_source
        return solid_cad_source(project.mechanical.cad_operations)
    width, depth, height = _project_dimensions(project)
    mechanical = project.mechanical
    enclosure_text = " ".join(
        (
            str(mechanical.physical_form if mechanical is not None else ""),
            str(mechanical.enclosure_type if mechanical is not None else ""),
        )
    ).lower()
    open_frame = "open" in enclosure_text and "enclosure" not in enclosure_text
    wall = max(1.5, min(3.0, min(width, depth, height) / 12.0))
    placements = json.dumps(_placement_payload(project), sort_keys=True)
    motion_intents = repr(_motion_intent_payload(project))
    return """from opencad import Part, Sketch, get_default_context


def xy_prism(x, y, z, length, width, height, name):
    profile = Sketch(plane="XY", origin=(0.0, 0.0, z), name=name + " profile")
    profile.rect(length, width, origin=(x, y))
    return Part(name=name).extrude(profile, depth=height, name=name)


def xz_prism(x, y, z, width, height, depth, name):
    profile = Sketch(plane="XZ", origin=(0.0, y, 0.0), name=name + " profile")
    profile.rect(width, height, origin=(x, z))
    return Part(name=name).extrude(profile, depth=depth, name=name)


def yz_prism(x, y, z, width, height, depth, name):
    profile = Sketch(plane="YZ", origin=(x, 0.0, 0.0), name=name + " profile")
    profile.rect(width, height, origin=(y, z))
    return Part(name=name).extrude(profile, depth=depth, name=name)


def xz_round_prism(x, y, z, radius, depth, name):
    profile = Sketch(plane="XZ", origin=(0.0, y, 0.0), name=name + " profile")
    profile.circle(radius, center=(x, z))
    return Part(name=name).extrude(profile, depth=depth, name=name)


def clamp(value, lower, upper):
    return max(lower, min(value, upper))


WIDTH = %r
DEPTH = %r
HEIGHT = %r
WALL = %r
OPEN_FRAME = %r
PLACEMENTS = %s
MOTION_INTENTS = %s
MOVING_REFS = {intent["target_ref"] for intent in MOTION_INTENTS}
PARTS = {}

if OPEN_FRAME:
    model = xy_prism(-WIDTH / 2.0, -DEPTH / 2.0, -HEIGHT / 2.0, WIDTH, DEPTH, WALL, "Open frame base")
else:
    outer = xy_prism(-WIDTH / 2.0, -DEPTH / 2.0, -HEIGHT / 2.0, WIDTH, DEPTH, HEIGHT, "Beacon outer envelope")
    cavity = xy_prism(
        -WIDTH / 2.0 + WALL,
        -DEPTH / 2.0 + WALL,
        -HEIGHT / 2.0 + WALL,
        WIDTH - 2.0 * WALL,
        DEPTH - 2.0 * WALL,
        HEIGHT,
        "Open interior cavity",
    )
    model = outer.cut(cavity, name="Enclosure tray")

display = next((item for item in PLACEMENTS if "display" in (item["category"] + " " + item["label"]).lower() or "oled" in item["label"].lower()), None)
if display and not OPEN_FRAME:
    opening_width = clamp(display["sx"], 8.0, WIDTH - 4.0 * WALL)
    opening_height = clamp(display["sz"], 8.0, HEIGHT - 4.0 * WALL)
    opening_x = clamp(display["x"] - opening_width / 2.0, -WIDTH / 2.0 + WALL, WIDTH / 2.0 - WALL - opening_width)
    opening_z = clamp(display["z"] - opening_height / 2.0, -HEIGHT / 2.0 + WALL, HEIGHT / 2.0 - WALL - opening_height)
    display_cutout = xz_prism(opening_x, -DEPTH / 2.0 - 1.0, opening_z, opening_width, opening_height, WALL + 2.0, "OLED display cutout")
    model = model.cut(display_cutout, name="Front display opening")

    bezel_width = min(WIDTH - 2.0 * WALL, opening_width + 8.0)
    bezel_height = min(HEIGHT - 2.0 * WALL, opening_height + 8.0)
    bezel = xz_prism(
        display["x"] - bezel_width / 2.0,
        -DEPTH / 2.0 - WALL - 1.0,
        display["z"] - bezel_height / 2.0,
        bezel_width,
        bezel_height,
        WALL + 1.5,
        "OLED bezel",
    )
    bezel_hole = xz_prism(
        opening_x,
        -DEPTH / 2.0 - WALL - 2.0,
        opening_z,
        opening_width,
        opening_height,
        WALL + 4.0,
        "OLED bezel window",
    )
    model = model.union(bezel.cut(bezel_hole, name="OLED bezel frame"), name="Display bezel connected")

power = next((item for item in PLACEMENTS if "usb" in (item["category"] + " " + item["label"]).lower() or "power" in item["category"].lower()), None)
if power and not OPEN_FRAME:
    usb_width = clamp(min(power["sx"], 20.0), 8.0, WIDTH - 4.0 * WALL)
    usb_height = clamp(min(power["sz"], 12.0), 6.0, HEIGHT - 4.0 * WALL)
    usb_x = clamp(power["x"] - usb_width / 2.0, -WIDTH / 2.0 + WALL, WIDTH / 2.0 - WALL - usb_width)
    usb_z = clamp(power["z"] - usb_height / 2.0, -HEIGHT / 2.0 + WALL, HEIGHT / 2.0 - WALL - usb_height)
    usb_cutout = xz_prism(usb_x, DEPTH / 2.0 - 1.0, usb_z, usb_width, usb_height, WALL + 2.0, "USB entry cutout")
    model = model.cut(usb_cutout, name="Rear USB opening")

sensors = [item for item in PLACEMENTS if "sensor" in (item["category"] + " " + item["label"]).lower()]
if sensors and not OPEN_FRAME:
    for side, side_x in (("left", -WIDTH / 2.0 - 1.0), ("right", WIDTH / 2.0 - WALL + 1.0)):
        for slot_index, slot_y in enumerate((-DEPTH * 0.30, -DEPTH * 0.10, DEPTH * 0.10, DEPTH * 0.30), start=1):
            vent = yz_prism(side_x, slot_y - DEPTH * 0.045, 0.0, DEPTH * 0.09, 2.5, WALL + 2.0, side + " vent " + str(slot_index))
            model = model.cut(vent, name=side.title() + " ventilation slot " + str(slot_index))

led = next((item for item in PLACEMENTS if "led" in (item["category"] + " " + item["label"]).lower() or "rgb" in item["label"].lower()), None)
if led and not OPEN_FRAME:
    light = xz_round_prism(led["x"], -DEPTH / 2.0 - 1.0, led["z"], min(4.0, led["sx"] / 2.0), WALL + 2.0, "Status light opening")
    model = model.cut(light, name="Status light cutout")

STATIC_ROOT_SHAPE_ID = model.shape_id

for item in PLACEMENTS:
    if "enclosure" in (item["category"] + " " + item["label"]).lower():
        continue
    rail_width = clamp(item["sx"], 8.0, WIDTH - 2.0 * WALL)
    rail_depth = clamp(min(item["sy"], 4.0), 2.0, DEPTH - 2.0 * WALL)
    rail_x = clamp(item["x"] - rail_width / 2.0, -WIDTH / 2.0 + WALL, WIDTH / 2.0 - WALL - rail_width)
    rail_y = clamp(item["y"] - rail_depth / 2.0, -DEPTH / 2.0 + WALL, DEPTH / 2.0 - WALL - rail_depth)
    rail = xy_prism(rail_x, rail_y, -HEIGHT / 2.0 - 0.5, rail_width, rail_depth, max(2.0, min(item["sz"] / 2.0, HEIGHT / 3.0)), item["ref_des"] + " mounting rail")
    model = model.union(rail, name=item["ref_des"] + " rail connected")

    component = xy_prism(
        item["x"] - item["sx"] / 2.0,
        item["y"] - item["sy"] / 2.0,
        item["z"] - item["sz"] / 2.0,
        item["sx"],
        item["sy"],
        item["sz"],
        item["ref_des"] + " component envelope",
    )
    PARTS[item["ref_des"]] = component
    if item["ref_des"] not in MOVING_REFS:
        model = model.union(component, name=item["ref_des"] + " placed component")

FORMA_EXPORT_SHAPE_IDS = [model.shape_id] + [
    PARTS[ref_des].shape_id
    for ref_des in sorted(MOVING_REFS)
    if ref_des in PARTS and PARTS[ref_des].shape_id
]

context = get_default_context()
for intent in MOTION_INTENTS:
    child = PARTS.get(intent["target_ref"])
    parent = PARTS.get(intent.get("parent_ref")) if intent.get("parent_ref") else None
    child_shape_id = child.shape_id if child is not None else None
    parent_shape_id = parent.shape_id if parent is not None else STATIC_ROOT_SHAPE_ID
    if not child_shape_id or not parent_shape_id:
        continue
    result = context.registry.call(
        "create_kinematic_joint",
        {
            "joint_id": intent["motion_id"],
            "type": intent["type"],
            "parent_shape_id": parent_shape_id,
            "child_shape_id": child_shape_id,
            "axis": intent["axis"],
            "origin_mm": intent["origin_mm"],
            "lower_limit": intent["lower_limit"],
            "upper_limit": intent["upper_limit"],
            "label": intent["label"],
            "metadata": {
                "forma_target_ref": intent["target_ref"],
                "forma_parent_ref": intent.get("parent_ref"),
                "notes": intent.get("notes"),
            },
        },
    )
    if not result.ok:
        raise RuntimeError("OpenCAD kinematic joint creation failed: " + result.message)

model
""" % (width, depth, height, wall, open_frame, placements, motion_intents)


def _adapter_path() -> Path:
    configured = config.optional("FORMA_CAD_ADAPTER_PATH")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        (
            Path(__file__).resolve().parents[3] / CAD_ADAPTER_RELATIVE_PATH,
            Path(__file__).resolve().parents[4] / CAD_ADAPTER_RELATIVE_PATH,
            Path.cwd() / CAD_ADAPTER_RELATIVE_PATH,
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CadGenerationError(
        "The Forma OpenCAD adapter is unavailable. Install the hardware skill or set "
        "FORMA_CAD_ADAPTER_PATH to its cad.py script."
    )


def _cad_workspace(project_id: str | None) -> Path:
    root = Path(config.optional("FORMA_CAD_WORKSPACE") or (Path.home() / "forma-workspace")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    project_key = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in str(project_id or uuid4())
    ).strip("._") or str(uuid4())
    project_root = root / project_key / "cad" / uuid4().hex
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "outputs").mkdir(exist_ok=True)
    return project_root


def _run_adapter(adapter: Path, model: Path, output: Path, tree: Path | None = None) -> dict[str, Any]:
    command = [sys.executable, str(adapter), "build", str(model), str(output), "--force"]
    if tree is not None:
        command.extend(("--tree-output", str(tree)))
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.number("FORMA_CAD_TIMEOUT_SECONDS", 900.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CadGenerationError(f"OpenCAD generation could not complete: {exc}") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = (completed.stderr or completed.stdout or "unknown OpenCAD error").strip()
        if completed.returncode != 0:
            raise CadGenerationError(f"OpenCAD generation failed: {detail[-1200:]}") from exc
        raise CadGenerationError("OpenCAD adapter returned invalid build metadata.") from exc
    if not isinstance(payload, dict) or not payload.get("valid"):
        raise CadGenerationError("OpenCAD adapter did not return a valid CAD artifact.")
    if completed.returncode != 0 and not output.is_file():
        detail = (completed.stderr or completed.stdout or "unknown OpenCAD error").strip()
        raise CadGenerationError(f"OpenCAD generation failed: {detail[-1200:]}")
    return payload


def _stl_mesh(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    vertices: list[float] = []
    faces: list[int] = []
    vertex_ids: dict[tuple[float, float, float], int] = {}

    def add_triangle(points: list[tuple[float, float, float]]) -> None:
        if len(points) != 3:
            return
        face = []
        for point in points:
            vertex_id = vertex_ids.setdefault(point, len(vertices) // 3)
            if vertex_id == len(vertices) // 3:
                vertices.extend(point)
            face.append(vertex_id)
        faces.extend(face)

    binary_count = int.from_bytes(data[80:84], "little") if len(data) >= 84 else -1
    if binary_count >= 0 and 84 + binary_count * 50 == len(data):
        for index in range(binary_count):
            offset = 84 + index * 50 + 12
            add_triangle([struct.unpack_from("<3f", data, offset + point * 12) for point in range(3)])
    else:
        points: list[tuple[float, float, float]] = []
        for line in data.decode("ascii", errors="replace").splitlines():
            fields = line.strip().split()
            if len(fields) == 4 and fields[0].lower() == "vertex":
                points.append(tuple(float(value) for value in fields[1:4]))
                if len(points) == 3:
                    add_triangle(points)
                    points = []
    if not faces:
        raise CadGenerationError("OpenCAD STL preview contains no triangles.")
    return {
        "shapeId": "native-cad-model",
        "name": "Forma OpenCAD model",
        "vertices": vertices,
        "faces": faces,
    }


def _stl_mesh_bytes(mesh: dict[str, Any]) -> bytes:
    """Serialize a preview triangle mesh as deterministic binary STL."""

    vertices = list(mesh.get("vertices") or [])
    faces = list(mesh.get("faces") or [])
    if len(vertices) % 3 or len(faces) % 3 or not vertices or not faces:
        raise CadGenerationError("OpenCAD preview mesh is not valid for STL export.")
    vertex_count = len(vertices) // 3
    output = bytearray(b"Forma portable STL".ljust(80, b"\0"))
    output.extend(struct.pack("<I", len(faces) // 3))
    for index in range(0, len(faces), 3):
        a, b, c = (int(faces[index + offset]) for offset in range(3))
        if min(a, b, c) < 0 or max(a, b, c) >= vertex_count:
            raise CadGenerationError("OpenCAD preview mesh contains an invalid STL face index.")
        coords: list[float] = []
        for vertex_id in (a, b, c):
            offset = vertex_id * 3
            coords.extend(float(vertices[offset + axis]) for axis in range(3))
        output.extend(struct.pack("<12fH", 0.0, 0.0, 0.0, *coords, 0))
    return bytes(output)


def _obj_mesh_bytes(mesh: dict[str, Any]) -> bytes:
    """Serialize the preview triangle mesh as a portable OBJ in millimeters."""

    vertices = list(mesh.get("vertices") or [])
    faces = list(mesh.get("faces") or [])
    if len(vertices) % 3 or len(faces) % 3 or not vertices or not faces:
        raise CadGenerationError("OpenCAD preview mesh is not valid for OBJ export.")
    lines = ["# Forma OpenCAD mesh", "# units: millimeter", "o assembly"]
    for index in range(0, len(vertices), 3):
        x, y, z = (float(vertices[index + offset]) for offset in range(3))
        lines.append(f"v {x:.9g} {y:.9g} {z:.9g}")
    vertex_count = len(vertices) // 3
    for index in range(0, len(faces), 3):
        a, b, c = (int(faces[index + offset]) for offset in range(3))
        if min(a, b, c) < 0 or max(a, b, c) >= vertex_count:
            raise CadGenerationError("OpenCAD preview mesh contains an invalid OBJ face index.")
        lines.append(f"f {a + 1} {b + 1} {c + 1}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _three_mf_mesh_bytes(mesh: dict[str, Any]) -> bytes:
    """Serialize the preview triangle mesh as a minimal standards-compliant 3MF package."""

    vertices = list(mesh.get("vertices") or [])
    faces = list(mesh.get("faces") or [])
    if len(vertices) % 3 or len(faces) % 3 or not vertices or not faces:
        raise CadGenerationError("OpenCAD preview mesh is not valid for 3MF export.")
    vertex_count = len(vertices) // 3
    vertex_xml = []
    for index in range(0, len(vertices), 3):
        x, y, z = (float(vertices[index + offset]) for offset in range(3))
        vertex_xml.append(f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>')
    triangle_xml = []
    for index in range(0, len(faces), 3):
        a, b, c = (int(faces[index + offset]) for offset in range(3))
        if min(a, b, c) < 0 or max(a, b, c) >= vertex_count:
            raise CadGenerationError("OpenCAD preview mesh contains an invalid 3MF face index.")
        triangle_xml.append(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')

    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<metadata name="Title">Forma assembly</metadata>'
        '<resources><object id="1" type="model"><mesh><vertices>'
        + "".join(vertex_xml)
        + '</vertices><triangles>'
        + "".join(triangle_xml)
        + '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>'
    ).encode("utf-8")
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        '</Types>'
    ).encode("utf-8")
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        '</Relationships>'
    ).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", relationships),
            ("3D/3dmodel.model", model),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return output.getvalue()


def _has_authoritative_cad(value: Any) -> bool:
    if value in (None, "", {}):
        return False
    return True


def _cad_is_applicable(project: HardwareIntermediateRepresentation) -> bool:
    if project.mechanical is None:
        return False
    return bool(project.mechanical.mechanism_benchmark or project.mechanical.cad_operations or project.mechanical.component_placements or project.components or project.mechanical.render_dimensions)


def _set_cad_status(project: HardwareIntermediateRepresentation, *, status: str, required: bool, error: str | None = None) -> None:
    metadata = dict(project.assembly_metadata or {})
    cad = project.cad_model if isinstance(project.cad_model, dict) else {}
    record: dict[str, Any] = {
        "adapter": str(cad.get("adapter") or CAD_ADAPTER_NAME),
        "status": status,
        "required": required,
    }
    if error:
        record["error"] = error[:500]
    metadata["cad_generation"] = record
    project.assembly_metadata = metadata


def _visual_gate_active(project: HardwareIntermediateRepresentation) -> bool:
    """Return whether the project explicitly selected progressive generation."""

    return is_progressive_generation(project)


def _prepare_visual_gate(project: HardwareIntermediateRepresentation):
    metadata = project.assembly_metadata or {}
    raw_policy = metadata.get("visual_approval_policy")
    policy = None
    if raw_policy:
        try:
            policy = VisualApprovalPolicy(str(raw_policy))
        except ValueError as exc:
            raise CadGenerationError(f"Unknown visual approval policy: {raw_policy!r}.") from exc
    return bootstrap_design_lifecycle(project, policy=policy)


def _safe_component_slug(ref_des: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(ref_des or "component")).strip("-") or "component"


def _component_cad_source(payload: dict[str, Any]) -> str:
    """Create a deterministic local-space envelope for one component placement."""

    sx = max(1.0, float(payload["sx"]))
    sy = max(1.0, float(payload["sy"]))
    sz = max(1.0, float(payload["sz"]))
    name = str(payload.get("label") or payload.get("ref_des") or "Component")
    return """from opencad import Part, Sketch

SX = %r
SY = %r
SZ = %r
NAME = %r

profile = Sketch(plane="XY", origin=(0.0, 0.0, -SZ / 2.0), name=NAME + " profile")
profile.rect(SX, SY, origin=(-SX / 2.0, -SY / 2.0))
model = Part(name=NAME).extrude(profile, depth=SZ, name=NAME)
model
""" % (sx, sy, sz, name)


def _component_cad_artifacts(
    project: HardwareIntermediateRepresentation,
    *,
    root: Path,
    adapter: Path,
    project_id: str | None,
) -> list[dict[str, Any]]:
    """Generate or reuse independently fingerprinted component CAD before assembly."""

    placements = _placement_payload(project)
    if not placements:
        return []

    component_dir = root / "components"
    output_dir = root / "outputs" / "components"
    component_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    lifecycle = load_design_lifecycle(project)
    topology_id = stable_artifact_id("project", RepresentationKind.TOPOLOGY)
    component_lookup = {component.ref_des: component for component in project.components}
    records: list[dict[str, Any]] = []

    for placement in placements:
        ref_des = str(placement["ref_des"])
        slug = _safe_component_slug(ref_des)
        component = component_lookup.get(ref_des)
        source_fingerprint = representation_fingerprint(
            placement,
            component.model_dump(mode="json") if component is not None else None,
        )
        artifact_id = stable_artifact_id(ref_des, RepresentationKind.COMPONENT_CAD)
        cached = lifecycle.by_id().get(artifact_id)
        if (
            cached is not None
            and cached.status == RepresentationStatus.READY
            and cached.source_fingerprint == source_fingerprint
            and cached.uri
            and Path(cached.uri).is_file()
        ):
            records.append({
                "ref_des": ref_des,
                "artifact_id": artifact_id,
                "source_fingerprint": source_fingerprint,
                "path": cached.uri,
                "reused": True,
                **cached.metadata,
            })
            continue

        model_path = component_dir / f"{slug}.py"
        step_path = output_dir / f"{slug}.step"
        tree_path = output_dir / f"{slug}.tree.json"
        model_path.write_text(_component_cad_source(placement), encoding="utf-8")
        summary = _run_adapter(adapter, model_path, step_path, tree_path)
        step_bytes = step_path.read_bytes()
        checksum = hashlib.sha256(step_bytes).hexdigest()
        if project_id:
            from forma_core.persistence.project_artifacts import ProjectArtifactStorage

            ProjectArtifactStorage().put(project_id, checksum, step_bytes, "model/step")
        representation = register_cad_representation(
            project,
            node_id=ref_des,
            kind=RepresentationKind.COMPONENT_CAD,
            source_fingerprint=source_fingerprint,
            uri=str(step_path),
            depends_on=[topology_id],
            metadata={
                "ref_des": ref_des,
                "sha256": checksum,
                "bytes": len(step_bytes),
                "model_source_path": str(model_path),
                "feature_tree_path": str(tree_path),
                "opencad_version": summary.get("opencad_version"),
                "project_id": project_id,
            },
        )
        lifecycle = load_design_lifecycle(project)
        records.append({
            "ref_des": ref_des,
            "artifact_id": representation.artifact_id,
            "source_fingerprint": source_fingerprint,
            "path": str(step_path),
            "sha256": checksum,
            "bytes": len(step_bytes),
            "reused": False,
        })
    return records


def ensure_native_cad_model(
    project: HardwareIntermediateRepresentation,
    *,
    project_id: str | None,
    required: bool,
    authoring_agent: str | None = None,
    workflow: str | None = None,
) -> bool:
    """Generate one-shot CAD by default, or hierarchical CAD in progressive mode."""

    explicit_solid = bool(project.mechanical and (project.mechanical.cad_operations or project.mechanical.mechanism_benchmark))
    if _has_authoritative_cad(project.cad_model) and not explicit_solid:
        _set_cad_status(project, status="provided", required=required)
        return False
    if not _cad_is_applicable(project):
        _set_cad_status(project, status="not_applicable", required=required)
        return False

    progressive = _visual_gate_active(project)
    if progressive:
        lifecycle = _prepare_visual_gate(project)
        gate = lifecycle.visual_gate
        allowed = (
            gate.policy != VisualApprovalPolicy.STOP_BEFORE_CAD
            and gate.status == VisualApprovalStatus.APPROVED
        )
        if not allowed:
            status = (
                "waiting_for_visual_approval"
                if gate.policy != VisualApprovalPolicy.STOP_BEFORE_CAD
                else "stopped_before_cad"
            )
            _set_cad_status(project, status=status, required=required)
            if workflow:
                from forma_core.agents.pipeline import emit_agent_pipeline_event

                emit_agent_pipeline_event(
                    workflow,
                    "cad_generation",
                    "deferred",
                    details={
                        "required": required,
                        "visual_approval_policy": gate.policy.value,
                        "visual_approval_status": gate.status.value,
                    },
                )
            return False

    if workflow:
        from forma_core.agents.pipeline import emit_agent_pipeline_event

        emit_agent_pipeline_event(workflow, "cad_generation", "started", details={"required": required})
    try:
        root = _cad_workspace(project_id)
        model_path = root / "assembly.py"
        step_path = root / "outputs" / "assembly.step"
        stl_path = root / "outputs" / "assembly.stl"
        tree_path = root / "outputs" / "assembly.tree.json"
        adapter = _adapter_path()

        component_artifacts: list[dict[str, Any]] = []
        if progressive:
            component_artifacts = _component_cad_artifacts(
                project,
                root=root,
                adapter=adapter,
                project_id=project_id,
            )

        assembly_source = _cad_source(project)
        model_path.write_text(assembly_source, encoding="utf-8")
        step_summary = _run_adapter(adapter, model_path, step_path, tree_path)
        _run_adapter(adapter, model_path, stl_path)
        step_bytes = step_path.read_bytes()
        checksum = hashlib.sha256(step_bytes).hexdigest()
        stl_bytes = stl_path.read_bytes()
        mesh = _stl_mesh(stl_path)
        obj_bytes = _obj_mesh_bytes(mesh)
        three_mf_bytes = _three_mf_mesh_bytes(mesh)
        obj_path = root / "outputs" / "assembly.obj"
        three_mf_path = root / "outputs" / "assembly.3mf"
        obj_path.write_bytes(obj_bytes)
        three_mf_path.write_bytes(three_mf_bytes)
        portable_exports = {
            "stl": {
                "filename": stl_path.name,
                "sha256": hashlib.sha256(stl_bytes).hexdigest(),
                "bytes": len(stl_bytes),
                "media_type": "model/stl",
            },
            "3mf": {
                "filename": three_mf_path.name,
                "sha256": hashlib.sha256(three_mf_bytes).hexdigest(),
                "bytes": len(three_mf_bytes),
                "media_type": "model/3mf",
            },
            "obj": {
                "filename": obj_path.name,
                "sha256": hashlib.sha256(obj_bytes).hexdigest(),
                "bytes": len(obj_bytes),
                "media_type": "model/obj",
            },
        }
        project.cad_model = {
            "adapter": CAD_ADAPTER_NAME,
            "source": "Native OpenCAD generated from agent-authored HardwareIntermediateRepresentation",
            "authoring_agent": authoring_agent,
            "authoring_mode": "component-cad-then-assembly" if progressive else "hardware-ir-to-opencad",
            "generated": True,
            "format": "step",
            "units": "mm",
            "filename": step_path.name,
            "path": str(step_path),
            "bytes": len(step_bytes),
            "sha256": checksum,
            "preview_filename": stl_path.name,
            "preview_path": str(stl_path),
            "model_source_path": str(model_path),
            "feature_tree_path": str(tree_path),
            "opencad_version": step_summary.get("opencad_version"),
            "kinematics": step_summary.get("kinematics"),
            "articulated_bodies": step_summary.get("articulated_bodies"),
            "compliant_preview": step_summary.get("compliant_preview"),
            "mechanism": step_summary.get("mechanism"),
            "exports": portable_exports,
            "meshes": [mesh],
        }
        if progressive:
            project.cad_model.update({
                "component_artifacts": component_artifacts,
                "component_artifact_ids": [item["artifact_id"] for item in component_artifacts],
            })
        if project_id:
            from forma_core.persistence.project_artifacts import ProjectArtifactStorage

            storage = ProjectArtifactStorage()
            storage.put(project_id, checksum, step_bytes, "model/step")
            for export_format, export_bytes in (
                ("stl", stl_bytes),
                ("3mf", three_mf_bytes),
                ("obj", obj_bytes),
            ):
                descriptor = portable_exports[export_format]
                storage.put(project_id, descriptor["sha256"], export_bytes, descriptor["media_type"])
            project.cad_model["stored_sha256"] = checksum
            project.cad_model["project_id"] = project_id

        if progressive:
            lifecycle = load_design_lifecycle(project)
            dependencies = [item["artifact_id"] for item in component_artifacts]
            if lifecycle.visual_gate.visual_artifact_id:
                dependencies.append(lifecycle.visual_gate.visual_artifact_id)
            assembly_representation = register_cad_representation(
                project,
                node_id="project",
                kind=RepresentationKind.ASSEMBLY_CAD,
                source_fingerprint=representation_fingerprint(
                    assembly_source,
                    [(item["artifact_id"], item["source_fingerprint"]) for item in component_artifacts],
                ),
                uri=str(step_path),
                depends_on=dependencies,
                metadata={
                    "sha256": checksum,
                    "bytes": len(step_bytes),
                    "preview_path": str(stl_path),
                    "model_source_path": str(model_path),
                    "feature_tree_path": str(tree_path),
                    "project_id": project_id,
                },
            )
            project.cad_model["assembly_artifact_id"] = assembly_representation.artifact_id

        _set_cad_status(project, status="succeeded", required=required)
        if workflow:
            from forma_core.agents.pipeline import emit_agent_pipeline_event

            details = {"adapter": CAD_ADAPTER_NAME, "format": "step", "bytes": len(step_bytes)}
            if progressive:
                details["component_artifact_count"] = len(component_artifacts)
            emit_agent_pipeline_event(
                workflow,
                "cad_generation",
                "completed",
                details=details,
            )
        return True
    except Exception as exc:
        if explicit_solid:
            project.cad_model = None
        _set_cad_status(project, status="failed", required=required, error=str(exc))
        if workflow:
            from forma_core.agents.pipeline import emit_agent_pipeline_event

            emit_agent_pipeline_event(
                workflow,
                "cad_generation",
                "failed" if required else "skipped",
                details={"required": required, "error": str(exc)[:500]},
            )
        if required:
            if isinstance(exc, CadGenerationError):
                raise
            raise CadGenerationError(str(exc)) from exc
        return False


def resume_native_cad_after_visual_decision(
    project: HardwareIntermediateRepresentation,
    *,
    project_id: str | None,
    approved: bool,
    feedback: str | None = None,
    required: bool = False,
    authoring_agent: str | None = None,
    workflow: str | None = None,
) -> bool:
    """Persist a visual decision and resume CAD only when policy permits it."""

    record_visual_decision(project, approved=approved, feedback=feedback)
    if not approved:
        _set_cad_status(project, status="visual_rejected", required=required)
        return False
    return ensure_native_cad_model(
        project,
        project_id=project_id,
        required=required,
        authoring_agent=authoring_agent,
        workflow=workflow,
    )


def cad_project_artifact(project: HardwareIntermediateRepresentation, project_id: str) -> ProjectArtifact | None:
    """Return the canonical revision artifact for a generated CAD model."""
    cad = project.cad_model
    if not isinstance(cad, dict) or str(cad.get("adapter") or "").strip().lower() != CAD_ADAPTER_NAME:
        return None
    checksum = str(cad.get("sha256") or "").strip()
    return ProjectArtifact(
        artifact_id="native-cad-model",
        kind="cad",
        uri=f"forma://projects/{project_id}/cad/assembly.step",
        media_type="model/step",
        checksum=f"sha256:{checksum}" if checksum and not checksum.startswith("sha256:") else checksum or None,
        metadata={
            "adapter": CAD_ADAPTER_NAME,
            "preview_path": cad.get("preview_path"),
            "model_source_path": cad.get("model_source_path"),
            "feature_tree_path": cad.get("feature_tree_path"),
            "format": cad.get("format", "step"),
            "component_artifact_ids": cad.get("component_artifact_ids") or [],
            "assembly_artifact_id": cad.get("assembly_artifact_id"),
        },
    )


def mesh_project_artifact(project: HardwareIntermediateRepresentation, project_id: str) -> ProjectArtifact | None:
    """Return the printable STL preview as a downstream fabrication input."""
    cad = project.cad_model
    if not isinstance(cad, dict):
        return None
    path = str(cad.get("preview_path") or "").strip()
    if not path:
        return None
    checksum = ""
    preview = Path(path)
    if preview.is_file():
        checksum = hashlib.sha256(preview.read_bytes()).hexdigest()
    return ProjectArtifact(
        artifact_id="native-cad-mesh",
        kind="mesh.stl",
        uri=str(preview),
        media_type="model/stl",
        checksum=f"sha256:{checksum}" if checksum else None,
        metadata={"path": str(preview), "source_project_id": project_id, "format": "stl"},
    )


__all__ = [
    "CAD_ADAPTER_NAME",
    "CadGenerationError",
    "cad_project_artifact",
    "ensure_native_cad_model",
    "mesh_project_artifact",
    "resume_native_cad_after_visual_decision",
]
