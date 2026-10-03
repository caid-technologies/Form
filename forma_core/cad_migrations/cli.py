"""Plan and package native rebuilds without sending CAD to a model provider."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .emitters import onshape_rebuild, python_rebuild
from .models import MigrationModel
from .planner import ROUTES, history_bytes, plan_migration


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register provider-neutral migration discovery, planning and rebuild package commands."""
    parser = subparsers.add_parser("cad-migrate", help="Validate CAD feature intent and generate native rebuild programs.")
    commands = parser.add_subparsers(dest="migration_command", required=True)
    for command in ("schema", "routes"):
        child = commands.add_parser(command)
        child.set_defaults(func=run)
    for command in ("plan", "build"):
        child = commands.add_parser(command)
        child.add_argument("history", type=Path)
        child.add_argument("--target", choices=("onshape", "nx", "fusion360"), required=True)
        child.add_argument("--approve-inferred", action="store_true", help="Acknowledge review of AI-inferred features; never overrides unsupported geometry or low confidence.")
        child.add_argument("--output", type=Path, required=command == "build", help="New report JSON or rebuild ZIP; existing files are rejected.")
        child.set_defaults(func=run)


def _json(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()


def build_migration(model: MigrationModel, target: str, *, approve_inferred: bool = False) -> bytes:
    """Build a deterministic package only when every inventory feature is mapped and reviewed."""
    report = plan_migration(model, target, approve_inferred=approve_inferred)
    if report["blockers"]:
        codes = sorted({item["code"] for item in report["blockers"]})
        raise ValueError("Migration blocked: " + ", ".join(codes) + ". Run cad-migrate plan for details.")
    normalized = _json(model.normalized())
    digest = hashlib.sha256(normalized).hexdigest()
    script_name = "rebuild.fs" if target == "onshape" else "rebuild.py"
    script = onshape_rebuild(model) if target == "onshape" else python_rebuild(target, digest)
    files = {"source-history.json": history_bytes(model), "rebuild.json": normalized,
             "plan.json": _json(report), script_name: script.encode()}
    instructions = {
        "onshape": "Create a Feature Studio in a new Onshape document. Paste rebuild.fs and commit it. Add its Forma migration custom feature to an empty Part Studio. Named lengths appear in the feature dialog. All source operations are inside that custom feature, not separate original feature-tree nodes.",
        "nx": "In a licensed NX session with Python/NX Open available, run rebuild.py through Journal > Play. Keep rebuild.json beside it. The journal creates a new unsaved millimeter part and named expressions. It uses native block/cylinder primitives, not the original source sketches.",
        "fusion360": "In Fusion Utilities > Scripts and Add-Ins, create a Python script and replace its Python file with rebuild.py; copy rebuild.json beside it. Run the script. It creates a new unsaved parametric design, named length parameters, dimensioned sketches and timeline extrusions.",
    }[target]
    files["README.md"] = ("# Forma AI-assisted CAD migration\n\n" + instructions + "\n\n"
        "The supplied source inventory, hash and feature evidence must come from your source CAD extraction or a reviewed reconstruction. "
        "Forma does not parse proprietary native files or infer missing source history by itself. "
        "The host agent may author the JSON contract; it cannot bypass schema, route, unsupported-feature or evidence checks.\n\n"
        "Read plan.json before execution. Run only in a new/scratch document. Preserve source-history.json and edit that contract, "
        "then regenerate to change intent; Python programs verify rebuild.json before doing any native work. "
        "Onshape FeatureScript is the inspectable rebuild artifact and contains its parameters directly.\n\n"
        "Native adapters require validation with your CAD version. A generated package is not a completed migration. "
        "After execution compare scale, orientation, solid count, bounding box and volume against the source; edit named dimensions "
        "to confirm history regenerates; check properties and save to a new native file. Material is reference text only. "
        "Python execution receipts explicitly report rebuilt_unverified or failed, never geometry equivalence.\n").encode()
    files["manifest.json"] = _json({"format": "forma-cad-migration", "version": 1, "target": target,
        "status": "rebuild_package_created", "native_execution_verified": False,
        "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)} for name, content in files.items()}})
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, content)
    return output.getvalue()


def run(args: argparse.Namespace) -> int:
    """Write JSON discovery/report output or a reviewed native reconstruction package."""
    if args.migration_command in {"schema", "routes"}:
        value = MigrationModel.model_json_schema() if args.migration_command == "schema" else [{"source": source, "target": target} for source, target in ROUTES]
        print(_json(value).decode(), end="")
        return 0
    if args.output and args.output.exists():
        raise ValueError("Output already exists; choose a new path")
    if args.history.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("History exceeds 2 MiB")
    model = MigrationModel.model_validate_json(args.history.read_bytes())
    report = plan_migration(model, args.target, approve_inferred=args.approve_inferred)
    if args.migration_command == "plan":
        if args.output:
            with args.output.open("xb") as output:
                output.write(_json(report))
        else:
            print(_json(report).decode(), end="")
        return 2 if report["blockers"] else 0
    bundle = build_migration(model, args.target, approve_inferred=args.approve_inferred)
    with args.output.open("xb") as output:
        output.write(bundle)
    print(_json({"status": "rebuild_package_created", "output": str(args.output),
                 "sha256": hashlib.sha256(bundle).hexdigest(), "native_execution_verified": False}).decode(), end="")
    return 0
