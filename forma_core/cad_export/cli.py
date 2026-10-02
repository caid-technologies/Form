"""Local Forma Export commands for people and host agents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .bundle import MAX_STEP_BYTES, TARGETS, CadMetadata, build_export, export_capabilities


def register_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the independent cad-export command."""
    parser = subparsers.add_parser("cad-export", help="Package STEP and metadata for professional CAD.")
    parser.add_argument("--capabilities", action="store_true", help="List targets as JSON without creating files.")
    parser.add_argument("--step", type=Path, help="Existing native STEP geometry to hand off.")
    parser.add_argument("--metadata", type=Path, help="Optional explicit CadMetadata JSON (not a full project/credentials file).")
    parser.add_argument("--target", choices=tuple(TARGETS))
    parser.add_argument("--output", type=Path, help="New ZIP path; existing files are never overwritten.")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Write a portable package with a machine-readable receipt, without uploading anything."""
    if args.capabilities:
        print(json.dumps(export_capabilities(), indent=2))
        return 0
    if not all((args.step, args.target, args.output)):
        raise ValueError("cad-export requires --step, --target and --output")
    if args.output.exists():
        raise ValueError("Output already exists; choose a new package path")
    if args.step.stat().st_size > MAX_STEP_BYTES:
        raise ValueError("STEP input exceeds 100 MiB")
    if args.metadata and args.metadata.stat().st_size > 1024 * 1024:
        raise ValueError("Metadata exceeds 1 MiB")
    metadata = CadMetadata.model_validate_json(args.metadata.read_bytes()) if args.metadata else CadMetadata()
    bundle = build_export(args.step.read_bytes(), args.target, metadata)
    with args.output.open("xb") as output:
        output.write(bundle)
    print(json.dumps({"status": "packaged", "target": args.target, "output": str(args.output),
                      "sha256": hashlib.sha256(bundle).hexdigest(), "native_history_preserved": False}))
    return 0
