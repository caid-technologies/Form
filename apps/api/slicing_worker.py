"""Server-only Ender 3 bridge. No CAD/slicer installation is needed in the cloud."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

PRINTER_ID = "creality_ender_3_04"
PROFILE_ID = "ender3"
SETTINGS = {"layer_height_mm": 0.2, "infill_percent": 15}
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_GCODE_BYTES = 64 * 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class WorkerError(RuntimeError):
    """Public-safe errors: never forward worker paths, responses, or credentials."""
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code, self.status = code, status


class SlicingWorker:
    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(url.strip())
        loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (not parsed.hostname or (parsed.scheme != "https" and not loopback)
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise WorkerError("slicing_worker_configuration", "Configure an HTTPS worker URL, or loopback for a local API.")
        if len(token) < 32 or any(c in token for c in "\r\n"):
            raise WorkerError("slicing_worker_configuration", "Configure the private slicing worker token (at least 32 characters).")
        self.url, self.token, self.transport = url.strip().rstrip("/"), token, transport

    def request(self, method: str, path: str, *, limit: int = 1024 * 1024, **kwargs: Any) -> tuple[bytes, httpx.Headers]:
        try:
            # Do not forward tokens through redirects, environment proxies, or browser URLs.
            with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(20, connect=5),
                              headers={"Authorization": f"Bearer {self.token}"}) as client:
                with client.stream(method, f"{self.url}{path}", **kwargs) as response:
                    if response.status_code == 404:
                        raise WorkerError("slice_job_not_found", "The slicing job is no longer available.", 404)
                    if not 200 <= response.status_code < 300:
                        raise WorkerError("slicing_worker_unavailable", "The slicing worker rejected the request. Check its connection and configuration.")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise WorkerError("slice_output_too_large", "The slicing worker response exceeds the demo limit.", 413)
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers
        except httpx.HTTPError as exc:
            raise WorkerError("slicing_worker_unavailable", "The mini-PC slicing worker is offline or unreachable.") from exc

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        import json
        body, _ = self.request(method, path, **kwargs)
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except (ValueError, UnicodeError) as exc:
            raise WorkerError("slicing_worker_protocol", "The slicing worker returned an invalid response.") from exc

    def capabilities(self) -> list[dict[str, Any]]:
        available, reason = True, None
        try:
            caps = self.json("GET", "/capabilities")
            profiles = caps.get("printers")
            profile = next((p for p in profiles if isinstance(p, dict) and p.get("id") == PROFILE_ID), None) if isinstance(profiles, list) else None
            if (caps.get("api_version") != 1 or not isinstance(caps.get("source_formats"), list)
                    or "step" not in caps["source_formats"]
                    or not profile or profile.get("slicer") != "prusaslicer"
                    or profile.get("missing_profile_files") != []):
                raise WorkerError("slicing_worker_profile", "The Ender 3 profile is not ready on the mini-PC.")
        except WorkerError as exc:
            available, reason = False, str(exc)
        return [{"printer_id": PRINTER_ID, "display_name": "Creality Ender-3",
                 "nozzle_mm": 0.4, "material": "PLA", "layer_height_mm": 0.2,
                 "available": available, "unavailable_reason": reason}]

    @staticmethod
    def checked_job(payload: dict[str, Any], project_id: str, digest: str) -> dict[str, Any]:
        job = payload.get("job")
        if not isinstance(job, dict):
            raise WorkerError("slicing_worker_protocol", "The slicing worker returned no job.")
        try:
            job_id = str(UUID(job.get("job_id", "")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkerError("slicing_worker_protocol", "The slicing worker returned an invalid job ID.") from exc
        if (job.get("project_id") != project_id or job.get("printer_profile") != PROFILE_ID
                or job.get("source_format") != "step" or job.get("material") != "pla"
                or job.get("settings") != SETTINGS
                or not str(job.get("idempotency_key", "")).startswith(f"forma-ender-v1:{digest}:")):
            raise WorkerError("slice_job_mismatch", "This job does not match the current project, STEP revision, or printer.", 409)
        if job.get("status") not in {"awaiting_upload", "queued", "running", "completed", "failed", "cancelled"}:
            raise WorkerError("slicing_worker_protocol", "The slicing worker returned an invalid status.")
        return {**job, "job_id": job_id}

    def start(self, project_id: str, digest: str, source: bytes) -> dict[str, Any]:
        if not _DIGEST.fullmatch(digest) or hashlib.sha256(source).hexdigest() != digest:
            raise WorkerError("step_integrity_failed", "The STEP artifact failed its integrity check.")
        if not source or len(source) > MAX_SOURCE_BYTES:
            raise WorkerError("step_too_large", "Use a nonempty STEP file under 20 MiB for this demo.", 413)
        request = {"project_id": project_id, "source_format": "step", "printer_profile": PROFILE_ID,
                   "material": "pla", "settings": dict(SETTINGS),
                   "idempotency_key": f"forma-ender-v1:{digest}:{uuid4().hex}"}
        job = self.checked_job(self.json("POST", "/slice", json=request), project_id, digest)
        if job["status"] != "awaiting_upload":
            raise WorkerError("slicing_worker_protocol", "A newly created slicing job was not ready for its upload.")
        # Ignore worker-supplied upload URLs; only use our fixed path and validated UUID.
        try:
            return self.checked_job(self.json("PUT", f"/jobs/{job['job_id']}/source", content=source,
                                              headers={"Content-Type": "application/octet-stream"}), project_id, digest)
        except WorkerError:
            try:
                self.request("DELETE", f"/jobs/{job['job_id']}")
            except WorkerError:
                pass
            raise

    def status(self, job_id: str, project_id: str, digest: str) -> dict[str, Any]:
        try:
            canonical = str(UUID(job_id))
        except ValueError as exc:
            raise WorkerError("slice_job_not_found", "Invalid slicing job ID.", 404) from exc
        job = self.checked_job(self.json("GET", f"/jobs/{canonical}"), project_id, digest)
        if job["job_id"] != canonical:
            raise WorkerError("slicing_worker_protocol", "The slicing worker returned the wrong job.")
        return job

    def artifact(self, job: dict[str, Any]) -> tuple[bytes, str]:
        if job["status"] != "completed" or job.get("slicer") != "prusaslicer":
            raise WorkerError("slice_output_unavailable", "The Ender 3 artifact is not ready.", 409)
        content, headers = self.request("GET", f"/jobs/{job['job_id']}/artifact", limit=MAX_GCODE_BYTES)
        digest = hashlib.sha256(content).hexdigest()
        if not content or headers.get("etag") != f'"sha256-{digest}"':
            raise WorkerError("gcode_integrity_failed", "The downloaded G-code failed its integrity check.")
        return content, digest
