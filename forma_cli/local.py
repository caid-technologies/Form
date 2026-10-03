"""Local project discovery and offline operations for ``forma-oss``."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import shutil
import struct
from typing import Any
from uuid import uuid4

from forma_core.config import config
from forma_core.workspaces.projects.manifest import (
    PROJECT_MANIFEST_FORMAT,
    ProjectArtifactReference,
    ProjectManifest,
    load_project_manifest,
    write_project_manifest,
)

from forma_cli.config import load_linkage, save_linkage
from forma_cli.credentials import CredentialStore, CredentialStoreError


PROJECT_FILENAME = "forma-project.json"
STRICT_GENERATION_ENVIRONMENT = {
    "FORMA_DISABLE_GENERATION_FALLBACK": "true",
    "FORMA_STRICT_GENERATION": "true",
    "LLM_DISABLE_FALLBACK": "true",
}
SIMULATION_ENVIRONMENT = {
    "FORMA_DEV_MODE": "true",
    "FORMA_DISABLE_GENERATION_FALLBACK": "false",
    "FORMA_STRICT_GENERATION": "false",
    "LLM_DISABLE_FALLBACK": "false",
}
ASSEMBLY_STEP_FILENAMES = ("assembly.step", "assembled.step")
LOCAL_OWNER_USER_ID = "local-dev-user"
LOCAL_PROVIDER_ENVIRONMENT = {
    "anthropic": "ANTHROPIC_API_KEY",
    "baseten": "BASETEN_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "firecrawl": "FIRECRAWL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "gmi": "GMI_API_KEY",
    "huggingface": "HF_TOKEN",
    "nvidia": "NVIDIA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "runpod": "RUNPOD_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "together": "TOGETHER_API_KEY",
    "xai": "XAI_API_KEY",
}


class LocalProjectError(RuntimeError):
    """Raised when a local project cannot be read or safely updated."""


def project_root(path: str | Path | None = None) -> Path:
    start = Path(path or Path.cwd()).expanduser().resolve()
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        if (candidate / PROJECT_FILENAME).is_file():
            return candidate
    raise LocalProjectError(f"Could not find {PROJECT_FILENAME} from {start}.")


def project_path(path: str | Path | None = None) -> Path:
    return project_root(path) / PROJECT_FILENAME


def read_project(path: str | Path | None = None) -> ProjectManifest:
    return load_project_manifest(project_path(path))


def init_project(path: str | Path | None = None, *, title: str = "") -> ProjectManifest:
    root = Path(path or Path.cwd()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / PROJECT_FILENAME
    if target.exists():
        raise LocalProjectError(f"A Forma project already exists at {target}.")
    manifest = ProjectManifest(
        format=PROJECT_MANIFEST_FORMAT,
        version=1,
        project_id=str(uuid4()),
        title=title.strip(),
        project_ir={},
    )
    write_project_manifest(target, manifest)
    save_linkage(root, {"version": 1, "project_id": manifest.project_id})
    return manifest


def _local_provider_environment(store: CredentialStore) -> dict[str, str]:
    values: dict[str, str] = {}
    for provider, environment_name in LOCAL_PROVIDER_ENVIRONMENT.items():
        try:
            value = store.get(f"provider:{provider}")
        except CredentialStoreError:
            # Local generation remains usable with environment-supplied keys.
            value = None
        if value:
            values[environment_name] = value
    return values


def _resolve_assembly_step_source(root: Path, requested: str | Path | None) -> Path | None:
    source = Path(requested).expanduser() if requested else None
    if source is None:
        source = next((root / name for name in ASSEMBLY_STEP_FILENAMES if (root / name).is_file()), None)
    if source is None:
        return None
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    if not source.is_file():
        raise LocalProjectError(f"Assembly STEP file does not exist: {source}")
    return source


def _prepare_assembly_step(root: Path, source: Path | None) -> Path | None:
    if source is None:
        return None

    destination = (root / "assembly.step").resolve()
    if source != destination:
        shutil.copy2(source, destination)
    return destination


def _resolve_source_file(root: Path, value: str | Path | None) -> Path | None:
    if not value:
        return None
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    if not source.is_file():
        raise LocalProjectError(f"Project artifact does not exist: {source}")
    return source


def _read_stl_mesh(path: Path) -> dict[str, Any]:
    """Read a binary or ASCII STL into the mesh payload consumed by the web viewport."""
    data = path.read_bytes()
    vertices: list[float] = []
    faces: list[int] = []
    vertex_ids: dict[tuple[float, float, float], int] = {}

    def add_triangle(points: list[tuple[float, float, float]]) -> None:
        if len(points) != 3:
            return
        face: list[int] = []
        for point in points:
            if not all(math.isfinite(value) for value in point):
                raise LocalProjectError(f"STL contains a non-finite vertex: {path}")
            vertex_id = vertex_ids.get(point)
            if vertex_id is None:
                vertex_id = len(vertices) // 3
                vertex_ids[point] = vertex_id
                vertices.extend(point)
            face.append(vertex_id)
        faces.extend(face)

    binary_count = int.from_bytes(data[80:84], "little") if len(data) >= 84 else -1
    binary_size = 84 + max(binary_count, 0) * 50
    if binary_count >= 0 and binary_size == len(data):
        for index in range(binary_count):
            offset = 84 + index * 50 + 12
            points = [struct.unpack_from("<3f", data, offset + point * 12) for point in range(3)]
            add_triangle(points)
    else:
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise LocalProjectError(f"Unsupported STL encoding: {path}") from exc
        points: list[tuple[float, float, float]] = []
        for line in text.splitlines():
            fields = line.strip().split()
            if len(fields) == 4 and fields[0].lower() == "vertex":
                try:
                    points.append(tuple(float(value) for value in fields[1:4]))
                except ValueError as exc:
                    raise LocalProjectError(f"Invalid STL vertex in {path}") from exc
                if len(points) == 3:
                    add_triangle(points)
                    points = []

    if not faces:
        raise LocalProjectError(f"STL contains no triangles: {path}")
    return {
        "shapeId": f"imported-{path.stem}",
        "name": path.stem,
        "vertices": vertices,
        "faces": faces,
    }


def import_project(
    source: str | Path,
    *,
    destination: str | Path | None = None,
    assembly_step: str | Path | None = None,
    preview_stl: str | Path | None = None,
) -> ProjectManifest:
    """Import a generated Hardware Intermediate Representation project and make its CAD renderable locally."""
    source_path = Path(source).expanduser().resolve()
    if source_path.is_dir():
        source_path = source_path / PROJECT_FILENAME
    if not source_path.is_file():
        raise LocalProjectError(f"Project manifest does not exist: {source_path}")

    source_root = source_path.parent.resolve()
    imported = load_project_manifest(source_path)
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

    try:
        ir = HardwareIntermediateRepresentation.model_validate(imported.project_ir)
    except Exception as exc:
        raise LocalProjectError(f"Project manifest contains invalid HardwareIntermediateRepresentation: {source_path}") from exc

    target_root = Path(destination).expanduser().resolve() if destination else source_root
    target_root.mkdir(parents=True, exist_ok=True)
    cad_model = ir.cad_model if isinstance(ir.cad_model, dict) else {}
    step_source = _resolve_source_file(source_root, assembly_step)
    if step_source is None:
        step_source = _resolve_source_file(source_root, cad_model.get("path"))
    if step_source is None:
        step_source = next(
            (candidate for name in ASSEMBLY_STEP_FILENAMES if (candidate := source_root / name).is_file()),
            None,
        )
    if step_source is None:
        raise LocalProjectError("Imported project has no native STEP artifact.")

    preview_source = _resolve_source_file(source_root, preview_stl)
    if preview_source is None:
        preview_source = _resolve_source_file(source_root, cad_model.get("preview_path"))
    if preview_source is None:
        raise LocalProjectError("Imported project has no STL preview artifact.")

    from forma_core.workspaces.projects.output import attach_assembly_step, persist_project_output

    assembly_path = _prepare_assembly_step(target_root, step_source)
    preview_path = (target_root / "cad-preview.stl").resolve()
    if preview_source != preview_path:
        shutil.copy2(preview_source, preview_path)
    ir.cad_model = {
        **cad_model,
        "preview_path": str(preview_path),
        "preview_filename": preview_path.name,
        "meshes": [_read_stl_mesh(preview_path)],
        "source": "Imported native OpenCAD STEP/STL project",
    }
    metadata = dict(ir.assembly_metadata or {})
    metadata["project_id"] = imported.project_id
    metadata["import_source"] = str(source_path)
    metadata["cad_preview_imported"] = True
    ir.assembly_metadata = metadata
    assembly_artifact = attach_assembly_step(ir, assembly_path)
    persist_project_output(
        ir,
        prompt_text=imported.prompt or imported.title,
        owner_user_id=LOCAL_OWNER_USER_ID,
    )

    artifacts = [
        artifact
        for artifact in imported.artifacts
        if artifact.path not in {"assembly.step", "assembled.step", "cad-preview.stl"}
    ]
    artifacts.extend(
        (
            ProjectArtifactReference(
                path="assembly.step",
                sha256=assembly_artifact["sha256"],
                media_type="model/step",
            ),
            ProjectArtifactReference(
                path="cad-preview.stl",
                sha256=hashlib.sha256(preview_path.read_bytes()).hexdigest(),
                media_type="model/stl",
            ),
        )
    )
    updated = ProjectManifest(
        format=PROJECT_MANIFEST_FORMAT,
        version=1,
        project_id=imported.project_id,
        workspace_id=imported.workspace_id,
        title=imported.title or (ir.overview.title if ir.overview else "Imported Forma Project"),
        prompt=imported.prompt or imported.title,
        project_ir=ir.model_dump(mode="json"),
        artifacts=artifacts,
    )
    write_project_manifest(target_root / PROJECT_FILENAME, updated)
    save_linkage(target_root, {"version": 1, "project_id": updated.project_id})
    return updated


def build_project(
    path: str | Path | None = None,
    *,
    prompt: str | None = None,
    workflow: str = "default",
    provider: str | None = None,
    model: str | None = None,
    simulation: bool = False,
    assembly_step: str | Path | None = None,
    credential_store: CredentialStore | None = None,
) -> ProjectManifest:
    root = project_root(path)
    current = read_project(root)
    assembly_source = _resolve_assembly_step_source(root, assembly_step)
    from forma_core.generation import generate_project_with_workflow

    requested_prompt = (prompt or current.prompt or current.title).strip()
    if not requested_prompt:
        raise LocalProjectError("Build requires a prompt. Pass one or set prompt in forma-project.json.")
    store = credential_store or CredentialStore()
    generation_environment = SIMULATION_ENVIRONMENT if simulation else STRICT_GENERATION_ENVIRONMENT
    environment = {**generation_environment, **_local_provider_environment(store)}
    with config.override(environment):
        ir = generate_project_with_workflow(
            workflow,
            requested_prompt,
            provider_name="simulation" if simulation else provider,
            model_name=None if simulation else model,
            generation_metadata={"project_id": current.project_id, "project_prompt": requested_prompt},
            persist_project=False,
        )
    metadata = dict(ir.assembly_metadata or {})
    metadata["project_id"] = current.project_id
    metadata["project_prompt"] = requested_prompt
    ir.assembly_metadata = metadata
    from forma_core.workspaces.projects.output import attach_assembly_step, persist_project_output

    assembly_path = _prepare_assembly_step(root, assembly_source)
    assembly_artifact = attach_assembly_step(ir, assembly_path) if assembly_path is not None else None
    persist_project_output(ir, prompt_text=requested_prompt, owner_user_id=LOCAL_OWNER_USER_ID)
    artifacts = [
        artifact
        for artifact in current.artifacts
        if artifact.path not in {"assembly.step", "assembled.step"}
    ]
    if assembly_artifact is not None:
        artifacts.append(
            ProjectArtifactReference(
                path="assembly.step",
                sha256=assembly_artifact["sha256"],
                media_type="model/step",
            )
        )
    updated = ProjectManifest(
        format=PROJECT_MANIFEST_FORMAT,
        version=1,
        project_id=current.project_id,
        workspace_id=current.workspace_id,
        title=current.title or (ir.overview.title if ir.overview else requested_prompt),
        prompt=requested_prompt,
        project_ir=ir.model_dump(mode="json"),
        artifacts=artifacts,
    )
    write_project_manifest(root / PROJECT_FILENAME, updated)
    return updated


def status_project(path: str | Path | None = None) -> dict[str, Any]:
    root = project_root(path)
    manifest = read_project(root)
    try:
        from forma_core.validation import build_validation_summary, validate_circuit
        from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

        ir = HardwareIntermediateRepresentation.model_validate(manifest.project_ir)
        summary = build_validation_summary(validate_circuit(ir.components, ir.nets, ir.requirements))
        valid = not summary.critical
        validation = summary.model_dump(mode="json")
    except Exception as exc:
        valid = False
        validation = {"critical": [str(exc)], "warning": [], "info": []}
    linkage = load_linkage(root)
    return {
        "project_id": manifest.project_id,
        "title": manifest.title,
        "path": str(root / PROJECT_FILENAME),
        "valid": valid,
        "validation": validation,
        "remote": linkage.get("remote"),
        "remote_project_id": linkage.get("remote_project_id") or linkage.get("project_id") if linkage.get("remote") else None,
        "revision_id": linkage.get("revision_id"),
        "parent_revision_id": linkage.get("parent_revision_id"),
    }


def slice_local_project(
    path: str | Path | None = None,
    *,
    printer_id: str | None = None,
    backend: str | None = None,
    profile: str | Path | None = None,
    profile_name: str | None = None,
    output_name: str | None = None,
) -> ProjectManifest:
    """Slice an existing local CAD mesh without regenerating the project."""
    root = project_root(path)
    manifest = read_project(root)
    from forma_core.workspaces.projects.fabrication import (
        SliceRequest,
        load_printer_registry,
        resolve_slice_profile,
        slice_project as run_slice_project,
    )
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
    from forma_core.workspaces.projects.state import ProjectArtifact

    try:
        ir = HardwareIntermediateRepresentation.model_validate(manifest.project_ir)
    except Exception as exc:
        raise LocalProjectError("Project manifest contains invalid HardwareIntermediateRepresentation.") from exc
    cad = ir.cad_model if isinstance(ir.cad_model, dict) else {}
    mesh_path = str(cad.get("preview_path") or "").strip()
    if not mesh_path:
        for artifact in manifest.artifacts:
            if (artifact.media_type or "").lower() == "model/stl":
                mesh_path = artifact.path
                break
    if not mesh_path:
        raise LocalProjectError("Project has no printable STL mesh artifact. Generate or import CAD first.")
    mesh = Path(mesh_path).expanduser()
    if not mesh.is_absolute():
        mesh = (root / mesh).resolve()
    if not mesh.is_file():
        raise LocalProjectError(f"Printable mesh artifact does not exist: {mesh}")
    registry = load_printer_registry()
    try:
        resolved = resolve_slice_profile(
            registry,
            printer_id=printer_id,
            backend=backend,
            native_config=str(profile) if profile else None,
            profile_name=profile_name,
        )
    except (ValueError, RuntimeError) as exc:
        raise LocalProjectError(str(exc)) from exc
    output = root / "fabrication" / (output_name or f"{mesh.stem}.gcode")
    if output_name and Path(output_name).name != output_name:
        raise LocalProjectError("Slice output name must be a filename, not a path.")
    mesh_artifact = ProjectArtifact(
        artifact_id="native-cad-mesh",
        kind="mesh.stl",
        uri=str(mesh),
        media_type="model/stl",
        checksum=f"sha256:{hashlib.sha256(mesh.read_bytes()).hexdigest()}",
        metadata={"path": str(mesh)},
    )
    try:
        result = run_slice_project(
            SliceRequest(
                mesh_artifact=mesh_artifact,
                profile=resolved,
                output_path=str(output),
                project_id=manifest.project_id,
            )
        )
    except Exception as exc:
        raise LocalProjectError(str(exc)) from exc

    metadata = dict(ir.assembly_metadata or {})
    metadata["fabrication"] = {
        "status": "succeeded",
        "backend": result.backend,
        "printer_name": result.printer_name,
        "profile_name": result.profile_name,
        "gcode_sha256": result.gcode_artifact.checksum,
        "report_uri": result.report_artifact.uri if result.report_artifact else None,
    }
    ir.assembly_metadata = metadata
    from forma_core.workspaces.projects.output import persist_project_output

    persist_project_output(ir, prompt_text=manifest.prompt or manifest.title, owner_user_id=LOCAL_OWNER_USER_ID)
    references = [
        artifact
        for artifact in manifest.artifacts
        if artifact.path not in {"fabrication/" + output.name, "fabrication/" + output.with_suffix(".report.json").name}
    ]
    for artifact in (result.gcode_artifact, result.report_artifact):
        if artifact is None:
            continue
        artifact_path = Path(artifact.uri).resolve().relative_to(root.resolve()).as_posix()
        references.append(
            ProjectArtifactReference(
                path=artifact_path,
                sha256=str(artifact.checksum or "").removeprefix("sha256:"),
                media_type=artifact.media_type,
                size_bytes=Path(artifact.uri).stat().st_size,
            )
        )
    updated = ProjectManifest(
        format=PROJECT_MANIFEST_FORMAT,
        version=1,
        project_id=manifest.project_id,
        workspace_id=manifest.workspace_id,
        title=manifest.title,
        prompt=manifest.prompt,
        visibility=manifest.visibility,
        project_ir=ir.model_dump(mode="json"),
        artifacts=references,
    )
    write_project_manifest(root / PROJECT_FILENAME, updated)
    return updated


def update_linkage(path: str | Path, **values: Any) -> dict[str, Any]:
    root = project_root(path)
    linkage = {**load_linkage(root), **values}
    save_linkage(root, linkage)
    return linkage


__all__ = [
    "LOCAL_PROVIDER_ENVIRONMENT",
    "LocalProjectError",
    "PROJECT_FILENAME",
    "build_project",
    "import_project",
    "init_project",
    "project_path",
    "project_root",
    "read_project",
    "status_project",
    "slice_local_project",
    "update_linkage",
]
