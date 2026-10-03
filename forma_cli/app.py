"""The first-party ``forma-oss`` command line application."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import quote
import webbrowser
from typing import Any

from forma_core import __version__
from forma_cli.config import api_url, load_linkage
from forma_cli.credentials import CredentialStore, CredentialStoreError
from forma_cli.local import (
    LOCAL_PROVIDER_ENVIRONMENT,
    LocalProjectError,
    build_project,
    import_project,
    init_project,
    project_root,
    read_project,
    slice_local_project,
    status_project,
    update_linkage,
)
from forma_cli.metadata_api import project_metadata, serve_metadata_api
from forma_cli.project_artifacts import (
    artifact_target,
    canonical_project_upload_payload,
    file_digest,
    prepare_project_upload,
)
from forma_cli.sdk import FormaAPIClient, FormaAPIError, ProjectArtifactDownload
from forma_core.config.compatibility import CompatibilityStatus
from forma_core.workspaces.projects.manifest import (
    ProjectManifest,
    normalize_artifact_media_type,
    validate_artifact_references,
    write_project_manifest,
    rewrite_artifact_paths,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _client(args: argparse.Namespace) -> FormaAPIClient:
    client = FormaAPIClient(base_url=args.api_url if getattr(args, "api_url", None) else None)
    notice = client.compatibility_notice()
    if notice:
        print(notice, file=sys.stderr)
    return client


def cmd_version(args: argparse.Namespace) -> int:
    result = _client(args).check_compatibility()
    payload = {
        "cli_version": __version__,
        "status": result.status.value,
        "latest_version": result.latest_version,
        "minimum_supported_version": result.minimum_supported_version,
        "protocol_version": result.protocol_version,
        "hardware_ir_version": result.hardware_ir_version,
        "message": result.message,
        "upgrade_command": result.upgrade_command,
    }
    if args.json:
        _print_json(payload)
    else:
        print("Forma-OSS")
        print(f"CLI:            {payload['cli_version']}")
        print(f"Latest:         {payload['latest_version'] or 'unavailable'}")
        print(f"Minimum:        {payload['minimum_supported_version'] or 'unavailable'}")
        print(f"Protocol:       {payload['protocol_version'] or 'unavailable'}")
        print(f"Hardware Intermediate Representation:    {payload['hardware_ir_version'] or 'unavailable'}")
        print(f"Status:         {payload['status']}")
        if result.status == CompatibilityStatus.UPDATE_AVAILABLE:
            print(f"Upgrade:        {result.upgrade_command}")
        elif result.status == CompatibilityStatus.REMOTE_VERSION_UNAVAILABLE:
            print(result.message)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    client = _client(args)
    compatibility = client.check_compatibility()
    checks: list[dict[str, str]] = [
        {
            "name": "Forma version",
            "status": "ok" if not compatibility.is_blocking else "fail",
            "message": compatibility.message,
        },
        {
            "name": "Hosted compatibility endpoint",
            "status": "ok" if compatibility.status != CompatibilityStatus.REMOTE_VERSION_UNAVAILABLE else "unavailable",
            "message": compatibility.status.value,
        },
    ]
    if client._saved_tokens():
        try:
            identity = client.whoami()
        except FormaAPIError as exc:
            checks.append({"name": "Authentication", "status": "fail", "message": str(exc)})
        else:
            checks.append({"name": "Authentication", "status": "ok", "message": identity.subject})
    else:
        checks.append({"name": "Authentication", "status": "not configured", "message": "Run `forma-oss login`."})
    if args.json:
        _print_json({"cli_version": __version__, "checks": checks})
    else:
        print("Forma-OSS doctor")
        for check in checks:
            print(f"{check['status'].upper():14} {check['name']}: {check['message']}")
    return 1 if any(check["status"] == "fail" for check in checks) else 0


def cmd_login(args: argparse.Namespace) -> int:
    client = _client(args)
    authorization = client.request_device_authorization()
    print("Opening Forma in your browser...")
    print(f"If the browser does not open, visit: {authorization.verification_uri}")
    if not args.no_browser:
        webbrowser.open(authorization.verification_uri)
    print("Waiting for authorization...")
    deadline = time.monotonic() + authorization.expires_in
    while time.monotonic() < deadline:
        result = client.poll_device_authorization(authorization.device_code)
        if result.status == "approved":
            client.exchange_device_code(authorization.device_code)
            identity = client.whoami()
            account = identity.email or identity.display_name or identity.subject
            print(f"Logged in as {account}")
            if client._store.using_plaintext_fallback:
                print("WARNING: credentials stored in plaintext because the explicit fallback is enabled.")
            else:
                print("Credentials stored securely")
            return 0
        if result.status in {"expired", "denied"}:
            raise FormaAPIError(result.message or f"Device authorization {result.status}.")
        time.sleep(authorization.interval)
    raise FormaAPIError("Device authorization expired before it was approved.")


def cmd_logout(args: argparse.Namespace) -> int:
    _client(args).revoke()
    print("Logged out")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    identity = _client(args).whoami()
    if args.json:
        _print_json(identity.model_dump(mode="json"))
    else:
        print(identity.email or identity.display_name or identity.subject)
        print(f"Provider: {identity.provider}")
        print(f"API endpoint: {identity.api_url}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    manifest = init_project(args.path, title=args.title or "")
    print(f"Initialized Forma project {manifest.project_id}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    manifest = build_project(
        args.path,
        prompt=args.prompt,
        workflow=args.workflow,
        provider=args.provider,
        model=args.model,
        simulation=args.simulation,
        assembly_step=args.assembly_step,
    )
    print(
        f"Built {manifest.title or manifest.project_id}, persisted {manifest.project_id} to Forma DB, "
        f"at {project_root(args.path) / 'forma-project.json'}"
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    source_path = Path(args.source).expanduser()
    destination = Path(args.path).expanduser() if args.path else (
        source_path if source_path.is_dir() else source_path.parent
    )
    manifest = import_project(
        args.source,
        destination=destination,
        assembly_step=args.assembly_step,
        preview_stl=args.preview_stl,
    )
    print(
        f"Imported {manifest.title or manifest.project_id}, persisted {manifest.project_id} to Forma DB, "
        f"at {destination / 'forma-project.json'}"
    )
    return 0


def cmd_metadata(args: argparse.Namespace) -> int:
    payload = project_metadata(args.path)
    if args.json:
        _print_json(payload)
    else:
        print(f"Project: {payload['title'] or payload['project_id']}")
        print(f"Project ID: {payload['project_id']}")
        print(f"Database: {'present' if payload['database']['present'] else 'missing'}")
        print(f"CAD: {payload['cad']['meshes']} mesh(es), {payload['cad']['mesh_vertices']} vertices")
        print(f"Components: {payload['hardware']['components']}")
        print(f"Placements: {payload['hardware']['placements']}")
        print(f"Valid: {'yes' if payload['valid'] else 'no'}")
    return 0


def cmd_metadata_api(args: argparse.Namespace) -> int:
    print(f"Serving project metadata at http://{args.host}:{args.port}/metadata")
    serve_metadata_api(args.path, args.host, args.port)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = status_project(args.path)
    if args.json:
        _print_json(payload)
    else:
        print(f"Project: {payload['title'] or payload['project_id']}")
        print(f"Path: {payload['path']}")
        print(f"Validation: {'ok' if payload['valid'] else 'failed'}")
        if payload.get("remote"):
            print(f"Remote: {payload['remote']}")
            print(f"Revision: {payload.get('revision_id') or 'none'}")
    return 0 if payload["valid"] else 1


def cmd_render(args: argparse.Namespace) -> int:
    from forma_core.terminal.dashboard import DashboardRenderConfig, render_dashboard_image

    root = project_root(args.path)
    manifest = read_project(root)
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = root / output
    render_dashboard_image(
        manifest.project_ir,
        output,
        config=DashboardRenderConfig(
            width=args.width,
            height=args.height,
            scene_yaw_degrees=args.yaw,
            scene_label="FORMA OSS / CLI RENDER",
        ),
    )
    print(f"Rendered {manifest.title or manifest.project_id} at {output}")
    return 0


def _printer_registry():
    from forma_core.workspaces.projects.fabrication import load_printer_registry

    return load_printer_registry()


def cmd_printer_list(args: argparse.Namespace) -> int:
    registry = _printer_registry()
    rows = [
        {
            "printer_id": printer.printer_id,
            "display_name": printer.display_name,
            "backend": printer.backend,
            "default_profile": printer.default_profile,
            "default": registry.default_printer == printer.printer_id,
            "profiles": sorted(printer.profiles),
        }
        for printer in sorted(registry.printers.values(), key=lambda item: item.printer_id)
    ]
    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            marker = "*" if row["default"] else " "
            print(f"{marker} {row['printer_id']}\t{row['backend']}\t{row['default_profile']}\t{row['display_name']}")
    return 0


def cmd_printer_show(args: argparse.Namespace) -> int:
    registry = _printer_registry()
    printer = registry.printers.get(args.printer_id)
    if printer is None:
        raise ValueError(f"Printer configuration not found: {args.printer_id}")
    payload = printer.model_dump(mode="json")
    payload["default"] = registry.default_printer == printer.printer_id
    if args.json:
        _print_json(payload)
    else:
        print(f"{printer.display_name} ({printer.printer_id})")
        print(f"Backend: {printer.backend}")
        print(f"Default profile: {printer.default_profile}")
        for name, profile in sorted(printer.profiles.items()):
            print(f"Profile {name}: {profile.native_config}")
    return 0


def cmd_printer_add(args: argparse.Namespace) -> int:
    from forma_core.workspaces.projects.fabrication import (
        PrinterProfileReference,
        UserPrinterConfig,
        save_printer_registry,
    )

    registry = _printer_registry()
    if args.printer_id in registry.printers:
        raise ValueError(f"Printer configuration already exists: {args.printer_id}")
    if not args.profiles:
        raise ValueError("At least one --profile NAME=PATH is required.")
    profiles = {}
    for value in args.profiles:
        name, separator, native_config = value.partition("=")
        if not separator or not name.strip() or not native_config.strip():
            raise ValueError("Profiles must use NAME=PATH format.")
        profiles[name.strip()] = PrinterProfileReference(native_config=native_config.strip())
    printer = UserPrinterConfig(
        printer_id=args.printer_id,
        display_name=args.display_name or args.printer_id,
        backend=args.backend,
        default_profile=args.default_profile or next(iter(profiles)),
        profiles=profiles,
    )
    registry.printers[printer.printer_id] = printer
    if args.set_default or registry.default_printer is None:
        registry.default_printer = printer.printer_id
    save_printer_registry(registry)
    print(f"Added printer {printer.printer_id}")
    return 0


def cmd_printer_set_default(args: argparse.Namespace) -> int:
    from forma_core.workspaces.projects.fabrication import save_printer_registry

    registry = _printer_registry()
    if args.printer_id not in registry.printers:
        raise ValueError(f"Printer configuration not found: {args.printer_id}")
    registry.default_printer = args.printer_id
    save_printer_registry(registry)
    print(f"Default printer: {args.printer_id}")
    return 0


def cmd_printer_remove(args: argparse.Namespace) -> int:
    from forma_core.workspaces.projects.fabrication import save_printer_registry

    registry = _printer_registry()
    if args.printer_id not in registry.printers:
        raise ValueError(f"Printer configuration not found: {args.printer_id}")
    del registry.printers[args.printer_id]
    if registry.default_printer == args.printer_id:
        registry.default_printer = next(iter(sorted(registry.printers)), None)
    save_printer_registry(registry)
    print(f"Removed printer {args.printer_id}")
    return 0


def cmd_project_slice(args: argparse.Namespace) -> int:
    if args.project_id:
        project_id = read_project(args.path).project_id
        if args.project_id != project_id:
            raise ValueError(f"Project ID mismatch: manifest contains {project_id}, requested {args.project_id}.")
    manifest = slice_local_project(
        args.path,
        printer_id=args.printer,
        backend=args.backend,
        profile=args.profile,
        profile_name=args.profile_name,
        output_name=args.output_name,
    )
    result = {
        "project_id": manifest.project_id,
        "operation": "slice",
        "artifacts": [artifact.model_dump(mode="json") for artifact in manifest.artifacts if artifact.path.startswith("fabrication/")],
    }
    if args.json:
        _print_json(result)
    else:
        print(f"Sliced project {manifest.project_id}")
        for artifact in result["artifacts"]:
            print(f"Wrote {artifact['path']}")
    return 0


def cmd_projects_list(args: argparse.Namespace) -> int:
    projects = _client(args).list_projects()
    if args.json:
        _print_json([project.model_dump(mode="json") for project in projects])
    else:
        for project in projects:
            revision = project.revision_id or "no revisions"
            print(f"{project.project_id}\t{project.title or 'Untitled'}\t{revision}")
    return 0


def _confirm_push(args: argparse.Namespace, manifest: Any) -> bool:
    output = sys.stderr if args.json else sys.stdout
    artifacts = list(getattr(manifest, "artifacts", []) or [])
    print(f"This will upload the private project manifest and {len(artifacts)} referenced artifact(s).", file=output)
    for artifact in artifacts:
        print(f"  - {artifact.path}", file=output)
    print("Provider credentials and authentication tokens are excluded.", file=output)
    if args.yes:
        return True
    if args.json:
        print("Continue? [y/N] ", end="", file=output, flush=True)
        answer = input().strip().lower()
    else:
        answer = input("Continue? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _artifact_summary(statuses: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in statuses:
        status = str(item.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(statuses),
        "succeeded": sum(value for key, value in counts.items() if key in {"uploaded", "restored", "already_present"}),
        "failed": sum(value for key, value in counts.items() if key not in {"uploaded", "restored", "already_present"}),
        "by_status": counts,
    }


def _linkage_artifact_digests(linkage: dict[str, Any]) -> dict[str, str]:
    value = linkage.get("artifact_digests")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(path): str(digest).strip().lower()
        for path, digest in value.items()
        if str(path).strip() and isinstance(digest, str) and digest.strip()
    }


def _ensure_pull_is_safe(
    root: Path,
    linkage: dict[str, Any],
    local_manifest: ProjectManifest,
    remote_artifacts: list[dict[str, Any]],
) -> None:
    """Reject dirty working trees before any remote bytes can replace files."""
    current_revision = linkage.get("revision_id")
    if current_revision:
        expected_digest = linkage.get("manifest_digest")
        if not expected_digest:
            raise LocalProjectError(
                "The linked project lacks a working-tree integrity record; refusing to overwrite local changes."
            )
        try:
            current_digest = _manifest_digest(canonical_project_upload_payload(root, local_manifest))
        except (LocalProjectError, ValueError) as exc:
            raise LocalProjectError(
                "Local project references cannot be verified; refusing to overwrite local changes."
            ) from exc
        if current_digest != expected_digest:
            raise LocalProjectError(
                "Local project changes diverge from the linked cloud revision; refusing to overwrite them."
            )
    elif (
        local_manifest.project_ir
        or local_manifest.artifacts
        or local_manifest.prompt
        or local_manifest.title not in {"", "Untitled Forma Project"}
    ):
        raise LocalProjectError(
            "The local project is not pristine and has no linked revision; refusing to overwrite local changes."
        )

    baseline_digests = _linkage_artifact_digests(linkage)
    local_digests = {
        artifact.path: artifact.sha256.strip().lower()
        for artifact in local_manifest.artifacts
        if artifact.sha256 and artifact.sha256.strip()
    }
    for artifact in remote_artifacts:
        target = artifact_target(root, artifact["path"])
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or target.is_dir():
            raise LocalProjectError(
                f"Local artifact target is not a regular file: {artifact['path']}; refusing to overwrite it."
            )
        actual_digest = file_digest(target)
        if actual_digest == artifact["sha256"]:
            continue
        baseline = baseline_digests.get(artifact["path"]) or local_digests.get(artifact["path"])
        if not baseline or actual_digest != baseline:
            raise LocalProjectError(
                f"Local artifact {artifact['path']} has changed; refusing to overwrite local changes."
            )


def _restored_manifest(
    root: Path,
    manifest: ProjectManifest,
    artifacts: list[dict[str, Any]],
) -> ProjectManifest:
    replacements = {
        artifact["path"]: str(artifact_target(root, artifact["path"]).resolve())
        for artifact in artifacts
    }
    payload = manifest.model_dump(mode="json")
    payload["artifacts"] = artifacts
    payload["project_ir"] = rewrite_artifact_paths(payload.get("project_ir", {}), replacements)
    return ProjectManifest.model_validate(payload)


def _download_parts(download: Any) -> tuple[bytes, str | None, str | None, int | None]:
    if isinstance(download, ProjectArtifactDownload):
        return download.content, download.content_type, download.sha256, download.size_bytes
    if isinstance(download, (bytes, bytearray)):
        return bytes(download), None, None, None
    if isinstance(download, tuple):
        content = bytes(download[0])
        content_type = download[1] if len(download) > 1 else None
        sha256 = download[2] if len(download) > 2 else None
        size_bytes = download[3] if len(download) > 3 else None
        return content, content_type, sha256, size_bytes
    if isinstance(download, dict):
        content = download.get("content", b"")
        return (
            bytes(content),
            download.get("content_type") or download.get("media_type"),
            download.get("sha256"),
            download.get("size_bytes"),
        )
    content = getattr(download, "content", b"")
    return (
        bytes(content),
        getattr(download, "content_type", None),
        getattr(download, "sha256", None),
        getattr(download, "size_bytes", None),
    )


def _push_idempotency_key(upload_payload: dict[str, Any]) -> str:
    return f"push:{_manifest_digest(upload_payload)}"


def cmd_projects_push(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    manifest = read_project(root)
    upload_payload, local_artifacts = prepare_project_upload(root, manifest)
    if not _confirm_push(args, manifest):
        if args.json:
            _print_json({"ok": False, "operation": "push", "status": "cancelled"})
        else:
            print("Upload cancelled.")
        return 1
    linkage = load_linkage(root)
    idempotency_key = args.key or _push_idempotency_key(upload_payload)
    client = _client(args)
    delivery = client.push_project(
        upload_payload,
        idempotency_key=idempotency_key,
        parent_revision_id=linkage.get("revision_id"),
        visibility=args.visibility,
    )
    if delivery.project.project_id != manifest.project_id:
        raise LocalProjectError("Cloud push returned a different project identity; refusing to update local linkage.")
    artifact_statuses: list[dict[str, Any]] = []
    for artifact in local_artifacts:
        try:
            content = artifact.source_path.read_bytes()
        except OSError as exc:
            raise LocalProjectError(f"Could not read project artifact {artifact.path}: {exc}") from exc
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise LocalProjectError(
                f"Project artifact {artifact.path} changed after validation; refusing to upload it."
            )
        try:
            response = client.upload_project_artifact(
                delivery.project.project_id,
                delivery.project.revision_id,
                artifact.sha256,
                content,
                artifact.media_type,
            )
        except FormaAPIError as exc:
            raise LocalProjectError(f"Could not upload project artifact {artifact.path}: {exc}") from exc
        response_sha256 = str(response.get("sha256") or artifact.sha256).strip().lower()
        response_media_type = str(response.get("media_type") or artifact.media_type)
        try:
            response_media_type = normalize_artifact_media_type(response_media_type)
        except ValueError as exc:
            raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.") from exc
        if response_sha256 != artifact.sha256 or response_media_type != artifact.media_type:
            raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.")
        response_size = response.get("size_bytes")
        if response_size is not None:
            try:
                if int(response_size) != artifact.size_bytes:
                    raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.")
            except (TypeError, ValueError) as exc:
                raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.") from exc
        artifact_statuses.append(
            {
                "path": artifact.path,
                "status": str(response.get("status") or "uploaded"),
                "sha256": artifact.sha256,
                "media_type": artifact.media_type,
                "size_bytes": artifact.size_bytes,
            }
        )
    try:
        completed = client.complete_delivery(delivery.delivery_id)
    except FormaAPIError as exc:
        raise LocalProjectError(
            f"Delivery session {delivery.delivery_id} could not be completed: {exc}"
        ) from exc
    if local_artifacts:
        # Persist the same portable, secret-free shape that was committed remotely.
        write_project_manifest(root / "forma-project.json", ProjectManifest.model_validate(upload_payload))
    update_linkage(
        root,
        version=1,
        remote=args.api_url.rstrip("/") if args.api_url else api_url(),
        remote_project_id=completed.project.project_id,
        project_id=manifest.project_id,
        revision_id=completed.project.revision_id,
        parent_revision_id=completed.project.parent_revision_id,
        manifest_digest=_manifest_digest(upload_payload),
        artifact_digests=json.dumps(
            {artifact.path: artifact.sha256 for artifact in local_artifacts},
            sort_keys=True,
        ),
    )
    payload = completed.model_dump(mode="json")
    payload["operation"] = "push"
    payload["idempotency_key"] = idempotency_key
    payload["artifacts"] = artifact_statuses
    payload["artifact_summary"] = _artifact_summary(artifact_statuses)
    payload["project_url"] = _project_url(client.base_url or api_url(), completed.project.project_id)
    if args.json:
        _print_json(payload)
    else:
        print(f"Delivered project {completed.project.project_id} revision {completed.project.revision_id}")
        print(f"Visibility: {completed.project.visibility}")
        print(f"Project URL: {payload['project_url']}")
    return 0


def cmd_projects_pull(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    linkage = load_linkage(root)
    remote_project_id = args.project_id or linkage.get("remote_project_id") or linkage.get("project_id")
    if not remote_project_id:
        raise LocalProjectError("No remote project is linked. Push this project first or pass --project-id.")
    client = _client(args)
    revision = client.pull_project(remote_project_id, args.revision_id)
    if revision.project_id != remote_project_id:
        raise LocalProjectError("Cloud pull returned a different project identity; refusing to update local files.")
    current_revision = linkage.get("revision_id")
    if current_revision and current_revision != revision.revision_id:
        if revision.parent_revision_id not in {current_revision, None}:
            raise LocalProjectError(
                "Cloud revision ancestry diverges from the linked local revision; refusing to overwrite it."
            )
    manifest = ProjectManifest.from_document(revision.manifest)
    if manifest.project_id != revision.project_id:
        raise LocalProjectError("Cloud revision manifest identity does not match the revision; refusing to pull it.")
    remote_artifacts = validate_artifact_references(manifest.artifacts, require_integrity=True)
    local_manifest = read_project(root)
    _ensure_pull_is_safe(root, linkage, local_manifest, remote_artifacts)

    artifact_statuses: list[dict[str, Any]] = []
    restored_manifest: ProjectManifest
    with tempfile.TemporaryDirectory(prefix="forma-pull-", dir=root) as temporary_dir:
        stage_root = Path(temporary_dir)
        for artifact in remote_artifacts:
            try:
                downloaded = client.download_project_artifact(
                    revision.project_id,
                    revision.revision_id,
                    artifact["sha256"],
                )
            except (FormaAPIError, OSError) as exc:
                raise LocalProjectError(f"Could not restore project artifact {artifact['path']}: {exc}") from exc
            content, response_media_type, response_sha256, response_size = _download_parts(downloaded)
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if actual_sha256 != artifact["sha256"] or (
                response_sha256 and str(response_sha256).lower() != artifact["sha256"]
            ):
                raise LocalProjectError(f"Downloaded project artifact {artifact['path']} failed its hash check.")
            if response_media_type:
                try:
                    normalized_media_type = normalize_artifact_media_type(response_media_type)
                except ValueError as exc:
                    raise LocalProjectError(f"Downloaded project artifact {artifact['path']} has an invalid media type.") from exc
                if normalized_media_type != artifact["media_type"]:
                    raise LocalProjectError(f"Downloaded project artifact {artifact['path']} failed its media type check.")
            if artifact.get("size_bytes") is not None and len(content) != artifact["size_bytes"]:
                raise LocalProjectError(f"Downloaded project artifact {artifact['path']} failed its size check.")
            if response_size is not None and int(response_size) != len(content):
                raise LocalProjectError(f"Downloaded project artifact {artifact['path']} returned an invalid size.")
            staged = stage_root / Path(*artifact["path"].split("/"))
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
            target = artifact_target(root, artifact["path"])
            artifact_statuses.append(
                {
                    "path": artifact["path"],
                    "status": "already_present" if target.is_file() and file_digest(target) == artifact["sha256"] else "restored",
                    "sha256": artifact["sha256"],
                    "media_type": artifact["media_type"],
                    "size_bytes": len(content),
                }
            )

        restored_manifest = _restored_manifest(root, manifest, remote_artifacts)
        for artifact in remote_artifacts:
            target = artifact_target(root, artifact["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage_root / Path(*artifact["path"].split("/"))).replace(target)
        write_project_manifest(root / "forma-project.json", restored_manifest)
    update_linkage(
        root,
        remote=args.api_url.rstrip("/") if args.api_url else api_url(),
        remote_project_id=revision.project_id,
        project_id=manifest.project_id,
        revision_id=revision.revision_id,
        parent_revision_id=revision.parent_revision_id,
        manifest_digest=_manifest_digest(canonical_project_upload_payload(root, restored_manifest)),
        artifact_digests=json.dumps(
            {artifact["path"]: artifact["sha256"] for artifact in remote_artifacts},
            sort_keys=True,
        ),
    )
    if args.json:
        payload = revision.model_dump(mode="json")
        payload["operation"] = "pull"
        payload["artifacts"] = artifact_statuses
        payload["artifact_summary"] = _artifact_summary(artifact_statuses)
        _print_json(payload)
    else:
        print(f"Pulled private project {revision.project_id} revision {revision.revision_id}")
        print(f"Restored {len(artifact_statuses)} project artifact(s)")
    return 0


def _delivery_idempotency_key(upload_payload: dict[str, Any]) -> str:
    return f"deliver:{_manifest_digest(upload_payload)}"


def cmd_projects_deliver(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    manifest = read_project(root)
    upload_payload, local_artifacts = prepare_project_upload(root, manifest)
    if not _confirm_push(args, manifest):
        if args.json:
            _print_json({"ok": False, "operation": "deliver", "status": "cancelled"})
        else:
            print("Deliver cancelled.")
        return 1
    linkage = load_linkage(root)
    idempotency_key = args.key or _delivery_idempotency_key(upload_payload)
    client = _client(args)
    delivery = client.deliver_project(
        upload_payload,
        idempotency_key=idempotency_key,
        parent_revision_id=linkage.get("revision_id"),
        visibility=args.visibility,
    )
    if delivery.project.project_id != manifest.project_id:
        raise LocalProjectError("Cloud delivery returned a different project identity; refusing to update local linkage.")
    artifact_statuses: list[dict[str, Any]] = []
    for artifact in local_artifacts:
        try:
            content = artifact.source_path.read_bytes()
        except OSError as exc:
            raise LocalProjectError(f"Could not read project artifact {artifact.path}: {exc}") from exc
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise LocalProjectError(
                f"Project artifact {artifact.path} changed after validation; refusing to upload it."
            )
        try:
            response = client.upload_project_artifact(
                delivery.project.project_id,
                delivery.project.revision_id,
                artifact.sha256,
                content,
                artifact.media_type,
            )
        except FormaAPIError as exc:
            raise LocalProjectError(f"Could not upload project artifact {artifact.path}: {exc}") from exc
        response_sha256 = str(response.get("sha256") or artifact.sha256).strip().lower()
        response_media_type = str(response.get("media_type") or artifact.media_type)
        try:
            response_media_type = normalize_artifact_media_type(response_media_type)
        except ValueError as exc:
            raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.") from exc
        if response_sha256 != artifact.sha256 or response_media_type != artifact.media_type:
            raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.")
        response_size = response.get("size_bytes")
        if response_size is not None:
            try:
                if int(response_size) != artifact.size_bytes:
                    raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.")
            except (TypeError, ValueError) as exc:
                raise LocalProjectError(f"Cloud artifact validation failed for {artifact.path}.") from exc
        artifact_statuses.append(
            {
                "path": artifact.path,
                "status": str(response.get("status") or "uploaded"),
                "sha256": artifact.sha256,
                "media_type": artifact.media_type,
                "size_bytes": artifact.size_bytes,
            }
        )
    try:
        completed = client.complete_delivery(delivery.delivery_id)
    except FormaAPIError as exc:
        raise LocalProjectError(
            f"Delivery session {delivery.delivery_id} could not be completed: {exc}"
        ) from exc
    if local_artifacts:
        write_project_manifest(root / "forma-project.json", ProjectManifest.model_validate(upload_payload))
    update_linkage(
        root,
        remote=args.api_url.rstrip("/") if args.api_url else api_url(),
        remote_project_id=completed.project.project_id,
        project_id=manifest.project_id,
        revision_id=completed.project.revision_id,
        parent_revision_id=completed.project.parent_revision_id,
        manifest_digest=_manifest_digest(upload_payload),
        artifact_digests=json.dumps(
            {artifact.path: artifact.sha256 for artifact in local_artifacts},
            sort_keys=True,
        ),
    )
    payload = completed.model_dump(mode="json")
    payload["operation"] = "deliver"
    payload["idempotency_key"] = idempotency_key
    payload["artifacts"] = artifact_statuses
    payload["project_url"] = _project_url(client.base_url or api_url(), completed.project.project_id)
    if args.json:
        _print_json(payload)
    else:
        print(f"Delivered project {completed.project.project_id} revision {completed.project.revision_id}")
        print(f"Visibility: {completed.project.visibility}")
        print(f"Project URL: {payload['project_url']}")
    return 0


def cmd_projects_publish(args: argparse.Namespace) -> int:
    root = project_root(args.path)
    linkage = load_linkage(root)
    project_id = args.project_id or linkage.get("remote_project_id") or linkage.get("project_id")
    if not project_id:
        raise LocalProjectError("No remote project is linked. Deliver or push it first, or pass --project-id.")
    result = _client(args).publish_project(project_id)
    payload = dict(result)
    payload["operation"] = "publish"
    if args.json:
        _print_json(payload)
    else:
        print(f"Project {project_id} is now {result.get('visibility', 'public')}")
        if result.get("published"):
            print(f"Published from {result.get('visibility_before')} by explicit action ({result.get('published_at')}).")
        else:
            print("Project was already public; no change was recorded.")
    return 0


def _manifest_digest(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _project_url(api_endpoint: str, project_id: str) -> str:
    """Build the browser URL without coupling the CLI to the web client."""
    web_endpoint = (
        os.environ.get("FORMA_WEB_URL")
        or os.environ.get("NEXT_PUBLIC_APP_URL")
        or api_endpoint.rstrip("/")
    ).rstrip("/")
    if web_endpoint.endswith("/api"):
        web_endpoint = web_endpoint[:-4].rstrip("/")
    return f"{web_endpoint}/project/{quote(project_id, safe='')}"


def _local_key_rows(store: CredentialStore) -> list[dict[str, Any]]:
    rows = []
    for provider in sorted(LOCAL_PROVIDER_ENVIRONMENT):
        try:
            configured = bool(store.get(f"provider:{provider}"))
        except CredentialStoreError:
            configured = False
        rows.append({"provider": provider, "scope": "local", "configured": configured})
    return rows


def cmd_keys_list(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    if args.scope in {"local", "both"}:
        rows.extend(_local_key_rows(CredentialStore()))
    if args.scope in {"cloud", "both"}:
        rows.extend(
            {**key.model_dump(mode="json"), "scope": "cloud"}
            for key in _client(args).list_keys()
        )
    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            masked = row.get("masked_value") or ("configured" if row.get("configured") else "not configured")
            print(f"{row['scope']}\t{row['provider']}\t{masked}")
    return 0


def cmd_keys_set(args: argparse.Namespace) -> int:
    if args.scope == "local" and args.provider not in LOCAL_PROVIDER_ENVIRONMENT:
        raise ValueError(
            f"Unknown local provider {args.provider!r}. Supported providers: "
            + ", ".join(sorted(LOCAL_PROVIDER_ENVIRONMENT))
        )
    value = args.value or getpass.getpass("Provider key (input hidden): ")
    if args.scope == "local":
        CredentialStore().set(f"provider:{args.provider}", value)
        print(f"Stored {args.provider} in the OS credential store for local generation.")
    else:
        result = _client(args).set_key(args.provider, value, label=args.label)
        print(f"Stored managed {result.provider} credential {result.credential_id or result.label or ''}".rstrip())
    return 0


def cmd_keys_remove(args: argparse.Namespace) -> int:
    if args.scope == "local" and args.provider not in LOCAL_PROVIDER_ENVIRONMENT:
        raise ValueError(
            f"Unknown local provider {args.provider!r}. Supported providers: "
            + ", ".join(sorted(LOCAL_PROVIDER_ENVIRONMENT))
        )
    if args.scope == "local":
        CredentialStore().delete(f"provider:{args.provider}")
        print(f"Removed local {args.provider} credential.")
    else:
        _client(args).remove_key(args.provider)
        print(f"Removed managed {args.provider} credential.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forma-oss", description="Local-first Forma project CLI.")
    parser.add_argument("--api-url", default=None, help="Forma API endpoint; defaults to local config or FORMA_API_URL.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser("version", help="Show local and hosted compatibility information.")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)
    doctor = subparsers.add_parser("doctor", help="Check local, hosted, and authentication compatibility.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    login = subparsers.add_parser("login", help="Authenticate with browser/device authorization.")
    login.add_argument("--no-browser", action="store_true", help="Print the approval URL without opening a browser.")
    login.set_defaults(func=cmd_login)
    logout = subparsers.add_parser("logout", help="Revoke the CLI session and remove local credentials.")
    logout.set_defaults(func=cmd_logout)
    whoami = subparsers.add_parser("whoami", help="Show the signed-in account and API endpoint.")
    whoami.add_argument("--json", action="store_true")
    whoami.set_defaults(func=cmd_whoami)

    init = subparsers.add_parser("init", help="Create a local forma-project.json.")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--title", default="")
    init.set_defaults(func=cmd_init)
    build = subparsers.add_parser("build", help="Generate or rebuild a project locally.")
    build.add_argument("prompt", nargs="?")
    build.add_argument("--path", default=".")
    build.add_argument("--workflow", default="default")
    build.add_argument("--provider")
    build.add_argument("--model")
    build.add_argument("--simulation", action="store_true")
    build.add_argument(
        "--assembly-step",
        help="Optional native STEP file to attach to the project.",
    )
    build.set_defaults(func=cmd_build)
    imported = subparsers.add_parser(
        "import",
        help="Import an existing generated Hardware Intermediate Representation project and its native CAD artifacts.",
    )
    imported.add_argument("source", help="Existing forma-project.json or its containing directory.")
    imported.add_argument("--path", default=None, help="Destination project directory; defaults to the source directory.")
    imported.add_argument("--assembly-step", help="Override the STEP artifact discovered from the source project.")
    imported.add_argument("--preview-stl", help="Override the STL preview discovered from the source project.")
    imported.set_defaults(func=cmd_import)
    metadata = subparsers.add_parser("metadata", help="Read local project and artifact metadata without the Forma API.")
    metadata.add_argument("--path", default=".")
    metadata.add_argument("--json", action="store_true")
    metadata.set_defaults(func=cmd_metadata)
    metadata_api = subparsers.add_parser("metadata-api", help="Serve local project metadata over a read-only HTTP API.")
    metadata_api.add_argument("--path", default=".")
    metadata_api.add_argument("--host", default="127.0.0.1")
    metadata_api.add_argument("--port", type=int, default=8765)
    metadata_api.set_defaults(func=cmd_metadata_api)
    status = subparsers.add_parser("status", help="Validate and show local/remote project state.")
    status.add_argument("--path", default=".")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    render = subparsers.add_parser("render", help="Render the local mechanical project layout to a PNG.")
    render.add_argument("--path", default=None)
    render.add_argument("--output", default="forma-render.png")
    render.add_argument("--width", type=int, default=1280)
    render.add_argument("--height", type=int, default=900)
    render.add_argument("--yaw", type=float, default=0.0)
    render.set_defaults(func=cmd_render)

    printer = subparsers.add_parser("printer", help="Manage the local user printer registry.")
    printer_commands = printer.add_subparsers(dest="printer_command", required=True)
    printer_add = printer_commands.add_parser("add", help="Register a printer and native slicing profile.")
    printer_add.add_argument("printer_id")
    printer_add.add_argument("--backend", required=True)
    printer_add.add_argument("--profile", dest="profiles", action="append", required=True, metavar="NAME=PATH")
    printer_add.add_argument("--display-name")
    printer_add.add_argument("--default-profile")
    printer_add.add_argument("--set-default", action="store_true")
    printer_add.set_defaults(func=cmd_printer_add)
    printer_list = printer_commands.add_parser("list", help="List registered printers.")
    printer_list.add_argument("--json", action="store_true")
    printer_list.set_defaults(func=cmd_printer_list)
    printer_show = printer_commands.add_parser("show", help="Show a registered printer.")
    printer_show.add_argument("printer_id")
    printer_show.add_argument("--json", action="store_true")
    printer_show.set_defaults(func=cmd_printer_show)
    printer_default = printer_commands.add_parser("set-default", help="Select the default printer.")
    printer_default.add_argument("printer_id")
    printer_default.set_defaults(func=cmd_printer_set_default)
    printer_remove = printer_commands.add_parser("remove", help="Remove a registered printer.")
    printer_remove.add_argument("printer_id")
    printer_remove.set_defaults(func=cmd_printer_remove)

    project = subparsers.add_parser("project", help="Run operations on a local Forma project.")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_slice = project_commands.add_parser("slice", help="Slice an existing project mesh into validated G-code.")
    project_slice.add_argument("project_id", nargs="?")
    project_slice.add_argument("--path", default=".")
    project_slice.add_argument("--printer")
    project_slice.add_argument("--backend")
    project_slice.add_argument("--profile", help="Native slicer profile/config path.")
    project_slice.add_argument("--profile-name")
    project_slice.add_argument("--output-name")
    project_slice.add_argument("--json", action="store_true")
    project_slice.set_defaults(func=cmd_project_slice)

    projects = subparsers.add_parser("projects", help="Manage explicit cloud project synchronization.")
    project_commands = projects.add_subparsers(dest="projects_command", required=True)
    projects_list = project_commands.add_parser("list", help="List private cloud projects.")
    projects_list.add_argument("--json", action="store_true")
    projects_list.set_defaults(func=cmd_projects_list)
    push = project_commands.add_parser("push", help="Upload the canonical manifest and referenced artifacts explicitly.")
    push.add_argument("--path", default=".")
    push.add_argument("--key", help="Idempotency key; defaults to a digest of the pushed manifest.")
    push.add_argument("--visibility", choices=("public", "private"), default=None)
    push.add_argument("--yes", action="store_true", help="Confirm upload without prompting.")
    push.add_argument("--json", action="store_true")
    push.set_defaults(func=cmd_projects_push)
    pull = project_commands.add_parser("pull", help="Download an exact or latest linked revision.")
    pull.add_argument("--path", default=".")
    pull.add_argument("--project-id")
    pull.add_argument("--revision-id")
    pull.add_argument("--json", action="store_true")
    pull.set_defaults(func=cmd_projects_pull)
    deliver = project_commands.add_parser(
        "deliver",
        help="Deliver a private snapshot with an idempotent receipt accepted only after artifacts are verified.",
    )
    deliver.add_argument("--path", default=".")
    deliver.add_argument("--key", help="Idempotency key; defaults to a digest of the delivered manifest.")
    deliver.add_argument("--visibility", choices=("public", "private"), default=None)
    deliver.add_argument("--yes", action="store_true", help="Confirm delivery without prompting.")
    deliver.add_argument("--json", action="store_true")
    deliver.set_defaults(func=cmd_projects_deliver)
    publish = project_commands.add_parser("publish", help="Explicitly publish an owned private project.")
    publish.add_argument("--path", default=".")
    publish.add_argument("--project-id")
    publish.add_argument("--json", action="store_true")
    publish.set_defaults(func=cmd_projects_publish)

    keys = subparsers.add_parser("keys", help="Manage local or hosted provider credentials.")
    key_commands = keys.add_subparsers(dest="keys_command", required=True)
    keys_list = key_commands.add_parser("list", help="List credential metadata only.")
    keys_list.add_argument("--scope", choices=("local", "cloud", "both"), default="local")
    keys_list.add_argument("--json", action="store_true")
    keys_list.set_defaults(func=cmd_keys_list)
    keys_set = key_commands.add_parser("set", help="Store a provider credential.")
    keys_set.add_argument("provider")
    keys_set.add_argument("--scope", choices=("local", "cloud"), default="local")
    keys_set.add_argument("--value", help=argparse.SUPPRESS)
    keys_set.add_argument("--label")
    keys_set.set_defaults(func=cmd_keys_set)
    keys_remove = key_commands.add_parser("remove", help="Remove a provider credential.")
    keys_remove.add_argument("provider")
    keys_remove.add_argument("--scope", choices=("local", "cloud"), default="local")
    keys_remove.set_defaults(func=cmd_keys_remove)
    return parser


def app(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CredentialStoreError, FormaAPIError, LocalProjectError, OSError, ValueError, RuntimeError) as exc:
        if getattr(args, "json", False):
            _print_json({"ok": False, "error": str(exc), "error_type": exc.__class__.__name__})
        else:
            print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 2


main = app


if __name__ == "__main__":
    raise SystemExit(app())
