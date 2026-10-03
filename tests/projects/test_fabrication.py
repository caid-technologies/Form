from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from forma_core.workspaces.projects.fabrication import (
    PrinterProfileReference,
    PrinterRegistry,
    SliceProfile,
    SliceRequest,
    SliceResult,
    UserPrinterConfig,
    load_printer_registry,
    resolve_slice_profile,
    save_printer_registry,
    slice_project,
)
from forma_core.workspaces.projects.fabrication.validation import validate_gcode
from forma_core.workspaces.projects.state import ProjectArtifact


class FakeSlicer:
    name = "fake"

    def is_available(self) -> bool:
        return True

    def inspect(self, mesh_path: Path) -> dict[str, object]:
        return {"valid": mesh_path.is_file()}

    def slice(self, request: SliceRequest) -> SliceResult:
        output = Path(request.output_path or "output.gcode")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("G90\nG1 X10 Y10 Z0.2 F1200\nG1 X20 Y10 E1.2 F600\n", encoding="ascii")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return SliceResult(
            gcode_artifact=ProjectArtifact(
                artifact_id="fake-gcode",
                kind="slice.gcode",
                uri=str(output),
                media_type="text/x-gcode",
                checksum=f"sha256:{digest}",
            ),
            backend="fake",
            profile_name=request.profile.profile_name,
            printer_name=request.profile.printer_name,
        )

    def validate(self, result: SliceResult):
        return validate_gcode(result.gcode_artifact.uri)


class FabricationTests(unittest.TestCase):
    def test_printer_registry_round_trips_dotted_profile_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "printers.toml"
            registry = PrinterRegistry(
                default_printer="ender-2-pro",
                printers={
                    "ender-2-pro": UserPrinterConfig(
                        printer_id="ender-2-pro",
                        display_name="Ender 2 Pro",
                        backend="cura",
                        default_profile="pla-0.2",
                        profiles={
                            "pla-0.2": PrinterProfileReference(
                                native_config="profiles/ender.json",
                                material="PLA",
                                bed_size_mm=(165, 165),
                            )
                        },
                    )
                },
            )
            save_printer_registry(registry, path)
            restored = load_printer_registry(path)

        profile = resolve_slice_profile(restored, printer_id="ender-2-pro")
        self.assertEqual("pla-0.2", profile.profile_name)
        self.assertEqual((165.0, 165.0), profile.bed_size_mm)
        self.assertEqual("cura", profile.backend)

    def test_validation_rejects_empty_or_out_of_bounds_gcode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.gcode"
            path.write_text("G1 X200 Y10 Z1\n", encoding="ascii")
            result = validate_gcode(
                path,
                SliceProfile(
                    backend="fake",
                    printer_name="Test printer",
                    bed_size_mm=(165, 165),
                ),
            )

        self.assertFalse(result.valid)
        self.assertTrue(any("extrusion" in error.lower() for error in result.errors))
        self.assertTrue(any("bounds" in error.lower() for error in result.errors))

    def test_slice_project_creates_report_and_validates_fake_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mesh = root / "assembly.stl"
            mesh.write_text("solid mesh\nendsolid mesh\n", encoding="ascii")
            result = slice_project(
                SliceRequest(
                    mesh_artifact=ProjectArtifact(
                        artifact_id="mesh",
                        kind="mesh.stl",
                        uri=str(mesh),
                        media_type="model/stl",
                        metadata={"path": str(mesh)},
                    ),
                    profile=SliceProfile(backend="fake", printer_name="Test printer"),
                    output_path=str(root / "fabrication" / "assembly.gcode"),
                ),
                adapter=FakeSlicer(),
            )

            self.assertTrue(result.validation and result.validation.valid)
            self.assertIsNotNone(result.report_artifact)
            self.assertEqual("slice.gcode", result.gcode_artifact.kind)
            self.assertTrue(Path(result.report_artifact.uri).is_file())


if __name__ == "__main__":
    unittest.main()
