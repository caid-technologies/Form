from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forma_core.workspaces.projects.fabrication.demo_printers import resolve_demo_slice_profile
from forma_core.workspaces.projects.fabrication.models import SliceProfile, SliceRequest
from forma_core.workspaces.projects.fabrication.slicers.orca import OrcaSlicerAdapter
from forma_core.workspaces.projects.state import ProjectArtifact


class DemoPrinterProfileTests(unittest.TestCase):
    """Verify fixed demo IDs resolve to real machine/process/filament profile files."""

    def _profile_tree(self, root: Path, *, vendor: str, machine_name: str, machine_file: str, process: str, filament: str) -> Path:
        """Create the minimum Orca system-profile tree required by the resolver."""
        executable = root / "bin" / "OrcaSlicer"
        executable.parent.mkdir(parents=True)
        executable.write_text("fake", encoding="utf-8")
        profile_root = executable.parent / "resources" / "profiles" / vendor
        for folder in ("machine", "process", "filament"):
            (profile_root / folder).mkdir(parents=True, exist_ok=True)
        (profile_root / "machine" / machine_file).write_text(
            json.dumps({
                "type": "machine",
                "name": machine_name,
                "default_print_profile": process,
                "default_filament_profile": [filament],
            }),
            encoding="utf-8",
        )
        (profile_root / "process" / f"{process}.json").write_text(
            json.dumps({"type": "process", "name": process}),
            encoding="utf-8",
        )
        (profile_root / "filament" / f"{filament}.json").write_text(
            json.dumps({"type": "filament", "name": filament}),
            encoding="utf-8",
        )
        return executable

    def test_ender_3_resolves_orca_system_profiles(self) -> None:
        """The stable Ender-3 demo ID resolves a 0.20 mm PLA Orca profile set."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._profile_tree(
                root,
                vendor="Creality",
                machine_name="Creality Ender-3 0.4 nozzle",
                machine_file="Creality Ender-3 0.4 nozzle.json",
                process="0.20mm Standard @Creality Ender3",
                filament="Creality Generic PLA",
            )
            profile = resolve_demo_slice_profile(
                "creality_ender_3_04",
                adapter=OrcaSlicerAdapter(executable=executable),
            )

        self.assertEqual("orca", profile.backend)
        self.assertEqual("Creality Ender-3", profile.printer_name)
        self.assertEqual((220.0, 220.0), profile.bed_size_mm)
        self.assertEqual(250.0, profile.z_height_mm)
        self.assertEqual(0.4, profile.nozzle_diameter_mm)
        self.assertEqual("PLA", profile.material)
        self.assertTrue(profile.native_config.endswith("Creality Ender-3 0.4 nozzle.json"))
        self.assertEqual(1, len(profile.native_settings))
        self.assertEqual(1, len(profile.native_filaments))

    def test_bambu_a1_resolves_orca_system_profiles(self) -> None:
        """The stable Bambu A1 demo ID resolves the vendor's 0.20 mm PLA presets."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._profile_tree(
                root,
                vendor="BBL",
                machine_name="Bambu Lab A1 0.4 nozzle",
                machine_file="Bambu Lab A1 0.4 nozzle.json",
                process="0.20mm Standard @BBL A1",
                filament="Bambu PLA Basic @BBL A1",
            )
            profile = resolve_demo_slice_profile(
                "bambu_a1_04",
                adapter=OrcaSlicerAdapter(executable=executable),
            )

        self.assertEqual("Bambu Lab A1", profile.printer_name)
        self.assertEqual((256.0, 256.0), profile.bed_size_mm)
        self.assertEqual(256.0, profile.z_height_mm)
        self.assertTrue(profile.require_temperatures)
        self.assertTrue(profile.require_extrusion)


class OrcaSlicerAdapterTests(unittest.TestCase):
    """Exercise CLI construction without requiring OrcaSlicer in CI."""

    def test_headless_slice_loads_machine_process_and_filament_profiles(self) -> None:
        """The adapter uses Orca's headless slice contract and captures generated G-code."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "OrcaSlicer"
            executable.write_text("fake", encoding="utf-8")
            mesh = root / "assembly.stl"
            mesh.write_text("solid demo\nendsolid demo\n", encoding="utf-8")
            machine = root / "machine.json"
            process = root / "process.json"
            filament = root / "filament.json"
            for path in (machine, process, filament):
                path.write_text("{}", encoding="utf-8")

            profile = SliceProfile(
                backend="orca",
                printer_name="Demo printer",
                profile_name="0.20mm Standard",
                native_config=str(machine),
                native_settings=[str(process)],
                native_filaments=[str(filament)],
                material="PLA",
                nozzle_diameter_mm=0.4,
            )
            request = SliceRequest(
                mesh_artifact=ProjectArtifact(
                    artifact_id="mesh",
                    kind="mesh.stl",
                    uri=str(mesh),
                    media_type="model/stl",
                    metadata={"path": str(mesh)},
                ),
                profile=profile,
                output_name="assembly-demo.gcode",
            )

            captured: list[str] = []

            def fake_run(command: list[str], **_: object):
                """Capture the command and emulate Orca's plate_1.gcode output."""
                captured.extend(command)
                output_dir = Path(command[command.index("--outputdir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "plate_1.gcode").write_text(
                    "M104 S210\nG90\nM83\nG1 X10 Y10 Z0.2 E0.5\n",
                    encoding="utf-8",
                )
                return type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

            adapter = OrcaSlicerAdapter(executable=executable)
            with patch("forma_core.workspaces.projects.fabrication.slicers.orca.subprocess.run", side_effect=fake_run):
                result = adapter.slice(request)

            output = Path(result.gcode_artifact.uri)
            self.assertTrue(output.is_file())
            self.assertEqual("assembly-demo.gcode", output.name)
            self.assertEqual("orca", result.backend)
            self.assertIn("--slice", captured)
            self.assertIn("--load-settings", captured)
            self.assertIn(f"{machine.resolve()};{process.resolve()}", captured)
            self.assertIn("--load-filaments", captured)
            self.assertIn(str(filament.resolve()), captured)


if __name__ == "__main__":
    unittest.main()
