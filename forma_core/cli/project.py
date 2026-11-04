"""Local adapters for the shared, inspectable project workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from forma_core.config import config
from forma_core.workspaces.projects.manifest import (
    ProjectArtifactReference, ProjectManifest, infer_artifact_media_type,
    load_project_manifest, normalize_artifact_path, rewrite_artifact_paths,
    validate_artifact_references, write_project_manifest,
)


def _json(value, output=None):
    from forma_core.cli.main import _write_json
    _write_json(value, output)


def _reader(root: Path):
    from forma_core.workspaces.projects.portable import local_artifact_reader
    return local_artifact_reader(root)


def _source(value: str):
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        path /= "forma-project.json"
    return path, load_project_manifest(path)


def _protect_report_output(output: str | None, *protected: Path) -> None:
    if output is None:
        return
    destination = Path(output).expanduser().resolve()
    if any(destination == path.expanduser().resolve() for path in protected):
        raise ValueError("Operation report must use a separate path from project source, artifacts and bundle files.")


def _project_files(path: Path, manifest: ProjectManifest) -> list[Path]:
    files = [path]
    for reference in manifest.artifacts:
        try:
            files.append(path.parent / normalize_artifact_path(reference.path))
        except ValueError:
            continue  # Inspection reports invalid declarations separately.
    return files


def _preserve_artifacts(manifest: ProjectManifest, source_root: Path, stage: Path,
                        generated: list[ProjectArtifactReference]) -> list[ProjectArtifactReference]:
    """Keep verified component/source attachments when rebuilding assembly CAD."""
    old_cad = manifest.project_ir.get("cad_model") or {}
    previous_outputs: set[str] = set()
    if isinstance(old_cad, dict):
        for key in ("path", "preview_path", "model_source_path", "feature_tree_path"):
            value = old_cad.get(key)
            if isinstance(value, str):
                try:
                    previous_outputs.add(normalize_artifact_path(value).casefold())
                except ValueError:
                    pass  # An old machine-local path is not a portable attachment.
        if isinstance(old_cad.get("path"), str):
            for suffix in (".obj", ".3mf"):
                try:
                    previous_outputs.add(normalize_artifact_path(str(Path(old_cad["path"]).with_suffix(suffix))).casefold())
                except ValueError:
                    pass
    generated_paths = {item.path.casefold() for item in generated}
    reserved = generated_paths | {"inspection.json", "forma-project.json"}
    reader = _reader(source_root)
    preserved: list[ProjectArtifactReference] = []
    for raw in validate_artifact_references([item.model_dump(mode="json") for item in manifest.artifacts]):
        reference = ProjectArtifactReference.model_validate(raw)
        key = reference.path.casefold()
        if key in previous_outputs and key in generated_paths:
            continue
        if key in reserved:
            raise ValueError(f"Attachment {reference.path} conflicts with generated output; rename it before compilation.")
        content = reader(reference)
        if hashlib.sha256(content).hexdigest() != reference.sha256 or (
            reference.size_bytes is not None and len(content) != reference.size_bytes
        ):
            raise ValueError(f"Attachment {reference.path} differs from its declared bytes; repair it before compilation.")
        target = stage / reference.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        preserved.append(reference)
    return preserved


def run(args: argparse.Namespace) -> int:
    from forma_core.workspaces.projects.inspection import (
        compile_project, inspect_project, record_project_review,
    )
    from forma_core.workspaces.projects.portable import PortableProjectManifest, export_project_bundle, import_project_bundle

    if args.action == "schema":
        from forma_core.workspaces.projects.inspection import ProjectInspection
        model = PortableProjectManifest if args.kind == "manifest" else ProjectInspection
        _json(model.model_json_schema(), args.output)
        return 0
    if args.action == "reopen":
        destination = Path(args.directory).expanduser().resolve()
        _protect_report_output(args.output, Path(args.bundle), destination / "forma-project.json",
                               destination / "forma-inspection.json")
        manifest = import_project_bundle(args.bundle, args.directory)
        _protect_report_output(args.output, *_project_files(destination / "forma-project.json", manifest))
        report = inspect_project(manifest, read_artifact=_reader(Path(args.directory).resolve()))
        _json(report, args.output)
        return 0 if report.is_valid else 1

    path, manifest = _source(args.project)
    _protect_report_output(args.output, *_project_files(path, manifest))
    if args.action == "inspect":
        report = inspect_project(manifest, read_artifact=_reader(path.parent))
        _json(report, args.output)
        return 0 if report.is_valid else 1
    if args.action == "export":
        _protect_report_output(args.output, Path(args.bundle))
        if Path(args.bundle).expanduser().resolve() in {item.expanduser().resolve() for item in _project_files(path, manifest)}:
            raise ValueError("Export bundle must use a separate path from project source and artifacts.")
        result = export_project_bundle(manifest, args.bundle, read_artifact=_reader(path.parent))
        _json({"bundle": str(result)}, args.output)
        return 0
    if args.action == "review":
        # A deliberate local declaration; this is not an authenticated sign-off.
        inspected = inspect_project(manifest, read_artifact=_reader(path.parent))
        if args.decision == "approved" and not inspected.is_valid:
            raise ValueError("Resolve invalid or unverifiable project artifacts before recording approval.")
        reviewed = record_project_review(manifest, decision=args.decision, reviewer=args.reviewer, note=args.note)
        write_project_manifest(path, reviewed)
        _json(inspect_project(reviewed, read_artifact=_reader(path.parent)), args.output)
        return 0

    destination = Path(args.directory).expanduser().resolve()
    protected = [destination / name for name in ("forma-project.json", "inspection.json", "assembly.py",
                 "assembly.tree.json", "assembly.step", "assembly.stl", "assembly.obj", "assembly.3mf")]
    for reference in manifest.artifacts:
        try:
            protected.append(destination / normalize_artifact_path(reference.path))
        except ValueError:
            continue
    _protect_report_output(args.output, *protected)
    if destination.exists():
        raise ValueError("Compile into a new directory to preserve the previous design and its artifacts.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    from forma_core.config.compatibility import ensure_supported_hardware_ir_version
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
    ensure_supported_hardware_ir_version(manifest.project_ir)
    project = HardwareIntermediateRepresentation.model_validate(manifest.project_ir)
    if not (project.mechanical and project.mechanical.cad_operations):
        raise ValueError("Portable compile requires explicit typed mechanical.cad_operations.")
    # All native output is staged; failed compilation never publishes a manifest.
    with tempfile.TemporaryDirectory(prefix=".forma-compile-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        with config.override({"FORMA_CAD_WORKSPACE": str(stage / "work"),
                              "FORMA_CLI_ARTIFACT_STORAGE_DIR": str(stage / "storage"),
                              "FORMA_CLI_ARTIFACT_STORAGE_BACKEND": "local"}):
            compiled = compile_project(project, project_id=None, required_cad=True)
        cad = compiled.cad_model or {}
        replacements = {}
        references = []
        files = [(cad.get("model_source_path"), "assembly.py"),
                 (cad.get("feature_tree_path"), "assembly.tree.json"),
                 (cad.get("path"), "assembly.step"),
                 (cad.get("preview_path"), "assembly.stl")]
        for value, name in files:
            if not value:
                raise ValueError(f"Native compilation did not produce {name}.")
            source = Path(value)
            target = stage / name
            shutil.copyfile(source, target)
            content = target.read_bytes()
            references.append(ProjectArtifactReference(path=name, sha256=hashlib.sha256(content).hexdigest(),
                              size_bytes=len(content), media_type=infer_artifact_media_type(name)))
            replacements[str(source)] = name
        # Additional formats are published only when their actual bytes exist.
        for suffix in ("obj", "3mf"):
            source = Path(cad["path"]).with_suffix("." + suffix)
            if source.is_file():
                target = stage / source.name
                shutil.copyfile(source, target)
                data = target.read_bytes()
                references.append(ProjectArtifactReference(path=target.name, sha256=hashlib.sha256(data).hexdigest(),
                                  size_bytes=len(data), media_type=infer_artifact_media_type(target.name)))
                replacements[str(source)] = target.name
        references.extend(_preserve_artifacts(manifest, path.parent, stage, references))
        payload = manifest.model_dump(mode="json")
        payload["project_ir"] = rewrite_artifact_paths(compiled.model_dump(mode="json"), replacements)
        payload["artifacts"] = [item.model_dump(mode="json") for item in references]
        result = ProjectManifest.model_validate(payload)
        report = inspect_project(result, read_artifact=_reader(stage))
        if not report.is_valid:
            raise ValueError("Compiled project failed deterministic inspection; no output was published.")
        write_project_manifest(stage / "forma-project.json", result)
        _json(report, str(stage / "inspection.json"))
        # Copy only declared deliverables, never transient build/storage files.
        publish = stage / "publish"
        publish.mkdir()
        for item in ["forma-project.json", "inspection.json", *[r.path for r in references]]:
            (publish / item).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage / item, publish / item)
        publish.rename(destination)
    _json({"project": str(destination / "forma-project.json"), "inspection": report.model_dump(mode="json")}, args.output)
    return 0


def register_parser(subparsers):
    parser = subparsers.add_parser("project", help="Compile, inspect, review and exchange portable projects.")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("inspect", "compile", "export", "review"):
        command = actions.add_parser(name)
        command.add_argument("project", help="Project manifest or raw HardwareIR JSON.")
        command.add_argument("--output", help="Write the operation report as JSON.")
        if name == "compile":
            command.add_argument("--directory", required=True, help="New output directory; existing projects are preserved.")
        if name == "export":
            command.add_argument("--bundle", required=True, help="Portable ZIP destination.")
        if name == "review":
            command.add_argument("--decision", required=True, choices=("approved", "rejected"))
            command.add_argument("--reviewer", required=True)
            command.add_argument("--note", default="")
        command.set_defaults(func=run)
    reopen = actions.add_parser("reopen")
    reopen.add_argument("bundle")
    reopen.add_argument("--directory", required=True)
    reopen.add_argument("--output")
    reopen.set_defaults(func=run)
    schema = actions.add_parser("schema")
    schema.add_argument("--kind", choices=("manifest", "inspection"), default="manifest")
    schema.add_argument("--output")
    schema.set_defaults(func=run)
