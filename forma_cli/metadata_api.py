"""Read-only local project metadata API for CLI verification."""

from __future__ import annotations

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from forma_core.database import get_project_identity

from forma_cli.local import project_root, read_project, status_project


def _file_metadata(root: Path, path: str | None, media_type: str | None = None) -> dict[str, Any]:
    raw_path = Path(path).expanduser() if path else None
    resolved = raw_path if raw_path and raw_path.is_absolute() else root / raw_path if raw_path else None
    resolved = resolved.resolve() if resolved else None
    exists = bool(resolved and resolved.is_file())
    payload: dict[str, Any] = {
        "path": str(resolved) if resolved else None,
        "relative_path": str(resolved.relative_to(root)) if resolved and resolved.is_relative_to(root) else path,
        "exists": exists,
    }
    if media_type:
        payload["media_type"] = media_type
    if exists and resolved:
        data = resolved.read_bytes()
        payload["bytes"] = len(data)
        payload["sha256"] = sha256(data).hexdigest()
    else:
        payload["bytes"] = 0
        payload["sha256"] = None
    return payload


def project_metadata(path: str | Path | None = None) -> dict[str, Any]:
    """Return verification metadata without making an HTTP/API request."""
    root = project_root(path)
    manifest = read_project(root)
    ir = manifest.project_ir if isinstance(manifest.project_ir, dict) else {}
    overview = ir.get("overview") if isinstance(ir.get("overview"), dict) else {}
    mechanical = ir.get("mechanical") if isinstance(ir.get("mechanical"), dict) else {}
    cad = ir.get("cad_model") if isinstance(ir.get("cad_model"), dict) else {}
    project_id = manifest.project_id

    try:
        stored = get_project_identity(project_id)
        database = {
            "present": stored is not None,
            "owner_user_id": stored.get("owner_user_id") if stored else None,
            "status": stored.get("status") if stored else None,
            "visibility": stored.get("visibility") if stored else None,
        }
    except Exception as exc:
        stored = None
        database = {"present": False, "error": str(exc)}

    artifacts = []
    for artifact in manifest.artifacts:
        artifacts.append(_file_metadata(root, artifact.path, artifact.media_type))

    cad_path = _file_metadata(root, cad.get("path"), "model/step") if cad else None
    preview_path = _file_metadata(root, cad.get("preview_path"), "model/stl") if cad else None
    meshes = cad.get("meshes") if isinstance(cad.get("meshes"), list) else []
    mesh_vertices = sum(
        len(mesh.get("vertices", []))
        for mesh in meshes
        if isinstance(mesh, dict) and isinstance(mesh.get("vertices"), list)
    )
    status = status_project(root)
    return {
        "project_id": project_id,
        "title": manifest.title or overview.get("title"),
        "prompt": manifest.prompt,
        "manifest_path": str(root / "forma-project.json"),
        "database": database,
        "hardware": {
            "components": len(ir.get("components", [])) if isinstance(ir.get("components"), list) else 0,
            "nets": len(ir.get("nets", [])) if isinstance(ir.get("nets"), list) else 0,
            "placements": len(mechanical.get("component_placements", [])),
        },
        "cad": {
            "present": bool(cad),
            "adapter": cad.get("adapter") if cad else None,
            "format": cad.get("format") if cad else None,
            "meshes": len(meshes),
            "mesh_vertices": mesh_vertices,
            "step": cad_path,
            "preview": preview_path,
        },
        "artifacts": artifacts,
        "validation": status["validation"],
        "valid": status["valid"],
    }


class _MetadataHandler(BaseHTTPRequestHandler):
    project_path: str | Path = "."

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path not in {"/metadata", "/metadata/"}:
            project_id = unquote(parsed.path.removeprefix("/metadata/")).strip("/")
            if project_id and project_id != project_metadata(self.project_path)["project_id"]:
                self.send_error(404, "Project not found")
                return
        try:
            payload = project_metadata(self.project_path)
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        except Exception as exc:
            body = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return None


def serve_metadata_api(path: str | Path | None, host: str, port: int) -> None:
    handler = type("ProjectMetadataHandler", (_MetadataHandler,), {"project_path": path or "."})
    with ThreadingHTTPServer((host, port), handler) as server:
        server.serve_forever()


__all__ = ["project_metadata", "serve_metadata_api"]
