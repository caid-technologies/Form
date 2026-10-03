"""Headless OrcaSlicer adapter for deterministic printer-profile slicing."""

from __future__ import annotations

import hashlib
import os
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


class OrcaSlicerAdapter:
    """Slice mesh artifacts with OrcaSlicer system printer/process profiles."""

    name = "orca"

    def __init__(self, executable: str | Path | None = None) -> None:
        """Create an adapter with an optional explicit OrcaSlicer executable."""
        self.executable = str(executable or config.optional("FORMA_ORCA_SLICER_PATH") or "").strip() or None

    @staticmethod
    def _standard_install_candidates() -> list[Path]:
        """Return common host-install locations not normally added to PATH."""
        candidates: list[Path] = []
        for variable in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
            root = str(os.environ.get(variable) or "").strip()
            if not root:
                continue
            base = Path(root)
            candidates.extend(
                [
                    base / "OrcaSlicer" / "orca-slicer.exe",
                    base / "OrcaSlicer" / "OrcaSlicer.exe",
                    base / "Programs" / "OrcaSlicer" / "orca-slicer.exe",
                    base / "Programs" / "OrcaSlicer" / "OrcaSlicer.exe",
                ]
            )
        candidates.extend(
            [
                Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
                Path("/usr/bin/orca-slicer"),
                Path("/usr/local/bin/orca-slicer"),
                Path("/opt/OrcaSlicer/orca-slicer"),
                Path("/opt/OrcaSlicer/OrcaSlicer"),
            ]
        )
        return candidates

    def executable_path(self) -> Path | None:
        """Return a configured, PATH-discovered, or conventionally installed OrcaSlicer binary."""
        if self.executable:
            configured = Path(self.executable).expanduser()
            return configured.resolve() if configured.is_file() else None
        discovered = (
            shutil.which("orca-slicer")
            or shutil.which("OrcaSlicer")
            or shutil.which("orca-slicer.exe")
            or shutil.which("OrcaSlicer.exe")
        )
        if discovered:
            return Path(discovered).resolve()
        return next((candidate.resolve() for candidate in self._standard_install_candidates() if candidate.is_file()), None)

    def is_available(self) -> bool:
        """Return whether OrcaSlicer can be invoked on this worker."""
        return self.executable_path() is not None

    def inspect(self, mesh_path: Path) -> SliceInspection:
        """Validate that the input is a non-empty mesh OrcaSlicer can consume."""
        if not mesh_path.is_file():
            return SliceInspection(valid=False, path=str(mesh_path), errors=[f"Mesh artifact does not exist: {mesh_path}"])
        if mesh_path.stat().st_size == 0:
            return SliceInspection(valid=False, path=str(mesh_path), errors=["Mesh artifact is empty."])
        valid = mesh_path.suffix.lower() in {".stl", ".3mf", ".obj"}
        return SliceInspection(
            valid=valid,
            path=str(mesh_path),
            errors=[] if valid else ["Mesh must be an STL, 3MF, or OBJ file."],
        )

    @staticmethod
    def _mesh_path(request: SliceRequest) -> Path:
        """Resolve the local input mesh path used by the OrcaSlicer subprocess."""
        raw = request.mesh_artifact.metadata.get("path") or request.mesh_artifact.uri
        if str(raw).startswith("forma://"):
            raise SlicerUnavailableError("A local mesh path is required to invoke OrcaSlicer.")
        return Path(str(raw)).expanduser().resolve()

    @staticmethod
    def _profile_paths(request: SliceRequest) -> tuple[list[Path], list[Path]]:
        """Resolve machine/process and filament profile paths from a slice profile."""
        settings = [request.profile.native_config, *request.profile.native_settings]
        setting_paths = [Path(value).expanduser().resolve() for value in settings if value]
        filament_paths = [Path(value).expanduser().resolve() for value in request.profile.native_filaments if value]
        if len(setting_paths) < 2:
            raise ValueError("OrcaSlicer requires a machine profile and a process profile.")
        if not filament_paths:
            raise ValueError("OrcaSlicer requires at least one filament profile.")
        missing = [path for path in [*setting_paths, *filament_paths] if not path.is_file()]
        if missing:
            raise ValueError(f"OrcaSlicer profile does not exist: {missing[0]}")
        return setting_paths, filament_paths

    def slice(self, request: SliceRequest) -> SliceResult:
        """Run a single-plate headless slice and return the generated G-code artifact."""
        executable = self.executable_path()
        if executable is None:
            raise SlicerUnavailableError(
                "OrcaSlicer is unavailable. Install OrcaSlicer or set FORMA_ORCA_SLICER_PATH."
            )
        mesh_path = self._mesh_path(request)
        inspection = self.inspect(mesh_path)
        if not inspection.valid:
            raise ValueError("; ".join(inspection.errors))
        setting_paths, filament_paths = self._profile_paths(request)

        if request.output_name and Path(request.output_name).name != request.output_name:
            raise ValueError("Slice output_name must be a filename, not a path.")
        output = Path(request.output_path).expanduser().resolve() if request.output_path else None
        output_dir = output.parent if output else mesh_path.parent / "fabrication"
        output_dir.mkdir(parents=True, exist_ok=True)
        requested_name = output.name if output else request.output_name or f"{mesh_path.stem}.gcode"
        if Path(requested_name).suffix.lower() not in {".gcode", ".gc"}:
            requested_name = f"{Path(requested_name).stem}.gcode"
        requested_output = output_dir / requested_name

        command = [
            str(executable),
            "--slice",
            "1",
            "--load-settings",
            ";".join(str(path) for path in setting_paths),
            "--load-filaments",
            ";".join(str(path) for path in filament_paths),
            "--outputdir",
            str(output_dir),
            str(mesh_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=config.number("FORMA_SLICER_TIMEOUT_SECONDS", 900.0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlicerUnavailableError(f"OrcaSlicer invocation failed: {exc}") from exc

        generated = sorted(
            path for path in output_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".gcode", ".gc"} and path.stat().st_size > 0
        )
        if completed.returncode != 0 or not generated:
            detail = (completed.stderr or completed.stdout or "unknown OrcaSlicer error").strip()
            raise RuntimeError(f"OrcaSlicer slicing failed: {detail[-1200:]}")

        generated_output = max(generated, key=lambda path: path.stat().st_size)
        if generated_output.resolve() != requested_output.resolve():
            if requested_output.exists():
                requested_output.unlink()
            generated_output.replace(requested_output)

        content = requested_output.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        artifact = ProjectArtifact(
            artifact_id=f"slice-gcode-{checksum[:16]}",
            kind="slice.gcode",
            uri=str(requested_output),
            media_type="text/x.gcode",
            checksum=f"sha256:{checksum}",
            metadata={
                "path": str(requested_output),
                "input_mesh": str(mesh_path),
                "input_mesh_sha256": hashlib.sha256(mesh_path.read_bytes()).hexdigest(),
                "slicer_executable": str(executable),
                "machine_profile": str(setting_paths[0]),
                "process_profiles": [str(path) for path in setting_paths[1:]],
                "filament_profiles": [str(path) for path in filament_paths],
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
        """Apply Forma's deterministic G-code checks to OrcaSlicer output."""
        from forma_core.workspaces.projects.fabrication.validation import validate_gcode

        return validate_gcode(Path(result.gcode_artifact.uri), profile=None)


__all__ = ["OrcaSlicerAdapter"]
