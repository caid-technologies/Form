"""CuraEngine adapter. Cura-specific subprocess details stay in this module."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

from forma_core.config import config
from forma_core.workspaces.projects.fabrication.models import (
    SliceInspection,
    SliceRequest,
    SliceResult,
    SliceValidationResult,
    SlicerUnavailableError,
)
from forma_core.workspaces.projects.state import ProjectArtifact


class CuraSlicerAdapter:
    name = "cura"

    def __init__(self, executable: str | Path | None = None) -> None:
        self.executable = str(executable or config.optional("FORMA_CURA_ENGINE_PATH") or "").strip() or None

    def executable_path(self) -> Path | None:
        if self.executable:
            configured = Path(self.executable).expanduser()
            return configured if configured.is_file() else None
        discovered = shutil.which("CuraEngine") or shutil.which("curaengine")
        return Path(discovered) if discovered else None

    def is_available(self) -> bool:
        return self.executable_path() is not None

    def inspect(self, mesh_path: Path) -> SliceInspection:
        if not mesh_path.is_file():
            return SliceInspection(valid=False, path=str(mesh_path), errors=[f"Mesh artifact does not exist: {mesh_path}"])
        valid = mesh_path.suffix.lower() in {".stl", ".3mf", ".obj"}
        return SliceInspection(
            valid=valid,
            path=str(mesh_path),
            errors=[] if valid else ["Mesh must be an STL, 3MF, or OBJ file."],
        )

    @staticmethod
    def _mesh_path(request: SliceRequest) -> Path:
        raw = request.mesh_artifact.metadata.get("path") or request.mesh_artifact.uri
        if str(raw).startswith("forma://"):
            raise SlicerUnavailableError("A local mesh path is required to invoke CuraEngine.")
        return Path(str(raw)).expanduser().resolve()

    def slice(self, request: SliceRequest) -> SliceResult:
        executable = self.executable_path()
        if executable is None:
            raise SlicerUnavailableError(
                "CuraEngine is unavailable. Install CuraEngine or set FORMA_CURA_ENGINE_PATH."
            )
        mesh_path = self._mesh_path(request)
        if not mesh_path.is_file() or mesh_path.suffix.lower() not in {".stl", ".3mf", ".obj"}:
            raise ValueError("Cura slicing requires an existing STL, 3MF, or OBJ mesh artifact.")
        if mesh_path.stat().st_size == 0:
            raise ValueError(f"Cura slicing requires a non-empty mesh artifact: {mesh_path}")
        if not request.profile.native_config:
            raise ValueError("Cura slicing requires an explicit native_config profile path.")
        native_config = Path(request.profile.native_config).expanduser().resolve()
        if not native_config.is_file():
            raise ValueError(f"Cura profile does not exist: {native_config}")
        output = Path(request.output_path).expanduser().resolve() if request.output_path else None
        if output is None:
            if request.output_name and Path(request.output_name).name != request.output_name:
                raise ValueError("Slice output_name must be a filename, not a path.")
            output_dir = mesh_path.parent / "fabrication"
            output = output_dir / (request.output_name or f"{mesh_path.stem}.gcode")
        if output.suffix.lower() not in {".gcode", ".gc"}:
            output = output.with_suffix(".gcode")
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [str(executable), "slice", "-j", str(native_config), "-l", str(mesh_path), "-o", str(output)]
        for setting in request.profile.native_settings:
            command.extend(("-s", setting))
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=config.number("FORMA_SLICER_TIMEOUT_SECONDS", 900.0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlicerUnavailableError(f"CuraEngine invocation failed: {exc}") from exc
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "unknown CuraEngine error").strip()
            raise RuntimeError(f"CuraEngine slicing failed: {detail[-1200:]}")
        content = output.read_bytes()
        artifact = ProjectArtifact(
            artifact_id=f"slice-gcode-{hashlib.sha256(content).hexdigest()[:16]}",
            kind="slice.gcode",
            uri=str(output),
            media_type="text/x.gcode",
            checksum=f"sha256:{hashlib.sha256(content).hexdigest()}",
            metadata={
                "path": str(output),
                "input_mesh": str(mesh_path),
                "input_mesh_sha256": hashlib.sha256(mesh_path.read_bytes()).hexdigest(),
                "slicer_executable": str(executable),
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            },
        )
        return SliceResult(
            gcode_artifact=artifact,
            backend=self.name,
            profile_name=request.profile.profile_name,
            printer_name=request.profile.printer_name,
        )

    def validate(self, result: SliceResult) -> SliceValidationResult:
        from forma_core.workspaces.projects.fabrication.validation import validate_gcode

        path = Path(result.gcode_artifact.uri)
        return validate_gcode(path, profile=None)


__all__ = ["CuraSlicerAdapter"]
