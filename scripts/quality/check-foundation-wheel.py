#!/usr/bin/env python3
"""Exercise installed packages from a fresh working directory using real CAD."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = args.fixture.resolve().read_bytes()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="forma-wheel-") as directory:
        root = Path(directory)
        (root / "source.json").write_bytes(fixture)

        def call(*arguments, expected=0):
            process = subprocess.run([sys.executable, "-I", "-m", "forma_core", "project", *map(str, arguments)],
                                     cwd=root, text=True, capture_output=True, timeout=180)
            if process.returncode != expected:
                raise RuntimeError(process.stderr + process.stdout)
            return json.loads(process.stdout)

        # Verify this interpreter resolved the installed distribution and adapter.
        import forma_core
        from forma_core.workspaces.projects.cad_generation import _adapter_path
        if "site-packages" not in str(Path(forma_core.__file__).resolve()):
            raise RuntimeError("Run wheel acceptance with installed packages, outside the source checkout.")
        if "_resources" not in _adapter_path().parts:
            raise RuntimeError("Wheel is missing its packaged native CAD adapter.")
        call("compile", root / "source.json", "--directory", root / "first")
        reviewed = call("review", root / "first", "--decision", "approved", "--reviewer", "Wheel acceptance",
                        "--note", "Test declaration; no physical qualification.")
        call("export", root / "first", "--bundle", output / "foundation-project.zip")
        reopened = call("reopen", output / "foundation-project.zip", "--directory", root / "reopened")
        if reviewed["manifest_sha256"] != reopened["manifest_sha256"]:
            raise RuntimeError("Project identity changed during portable export/reopen.")
        path = root / "reopened/forma-project.json"
        changed = json.loads(path.read_text())
        changed["project_ir"]["mechanical"]["cad_operations"][0]["size"]["x_mm"] = 60
        path.write_text(json.dumps(changed))
        stale = call("inspect", path, expected=1)
        if stale["human_review"]["status"] != "stale":
            raise RuntimeError("Design edits retained an old review declaration.")
        call("compile", path, "--directory", root / "second")
        import cadquery as cq
        first = cq.importers.importStep(str(root / "first/assembly.step")).val()
        second = cq.importers.importStep(str(root / "second/assembly.step")).val()
        if not first.isValid() or not second.isValid() or abs(first.BoundingBox().xlen - 40) > 1e-6 or abs(second.BoundingBox().xlen - 60) > 1e-6:
            raise RuntimeError("Exported native geometry did not reflect the edited dimensions.")
        report = {"installed_forma": forma_core.__version__, "adapter": str(_adapter_path()),
                  "first_width_mm": first.BoundingBox().xlen, "revised_width_mm": second.BoundingBox().xlen,
                  "portable_manifest_sha256": reopened["manifest_sha256"],
                  "review_after_edit": stale["human_review"]["status"], "status": "passed"}
        (output / "wheel-acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
