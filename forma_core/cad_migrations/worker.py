"""Customer-controlled pull worker; never run code received from a server.

The worker regenerates a supported target program from a validated history and
runs it in an installed licensed CAD session or an operator-configured runner.
Credentials stay in the worker environment; results contain model bytes only.
"""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

from .cli import build_migration, _json
from .models import MigrationModel

LIMIT = 50 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Worker API redirects are not allowed")


class WorkerClient:
    """Bound all authenticated requests to one operator-selected API origin."""
    def __init__(self, api: str, token: str, project: str):
        from uuid import UUID
        url = urlsplit(api)
        if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})) or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an HTTPS API URL (HTTP allowed only on loopback)")
        self.api, self.token = api.rstrip("/"), token
        self.path = "/projects/" + str(UUID(project)) + "/cad-workflows"
        self.opener = build_opener(NoRedirect())

    def request(self, path: str, data=None, binary=False):
        if not path.startswith(self.path):
            raise ValueError("Worker request escaped its project")
        body = data if binary else _json(data) if data is not None else None
        request = Request(self.api + path, data=body, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/zip" if binary else "application/json"})
        with self.opener.open(request, timeout=60) as response:
            content = response.read(LIMIT + 1)
        if len(content) > LIMIT:
            raise ValueError("Worker response is too large")
        return content if binary else json.loads(content)


def result_bundle(directory: Path) -> bytes:
    """Return only files named and hashed by one native evidence report."""
    reports = list(directory.glob("evidence-*.json"))
    if len(reports) != 1:
        raise ValueError("The native runner must produce exactly one evidence-*.json report")
    from .evidence import NativeEvidence
    evidence = NativeEvidence.model_validate_json(reports[0].read_bytes())
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("evidence.json", _json(evidence.model_dump(mode="json")))
        total = 0
        for name, digest in evidence.native_artifacts.items():
            path = directory / name
            if path.name != name or path.is_symlink() or not path.is_file():
                raise ValueError("Invalid native output filename")
            total += path.stat().st_size
            if total > LIMIT:
                raise ValueError("Native outputs exceed the upload limit")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Native output changed after measurement")
            archive.writestr(name, content)
    return output.getvalue()


def run_once(client: WorkerClient, target: str, execute) -> dict:
    """Claim one run, regenerate its fixed program, execute and return evidence/files.

    execute(directory, target) is a locally installed callback, never server code.
    No automatic execution retry: an operator must explicitly queue a new attempt.
    """
    snapshot = client.request(client.path)
    queued = next((r for r in snapshot["runs"] if r.get("target") == target and r.get("status") == "queued_for_worker" and not r.get("stale")), None)
    if queued is None:
        return {"status": "idle"}
    command = {"action": "claim", "request_id": str(uuid4()), "expected_revision": snapshot["revision"], "run_id": queued["id"], "worker_id": os.environ.get("FORMA_CAD_WORKER_ID", "customer-worker")}
    claimed = client.request(client.path, command)
    run = next(r for r in claimed["runs"] if r["id"] == queued["id"])
    try:
        artifact = run["artifacts"]["history"]
        content = client.request(client.path + "/artifacts/" + claimed["revision_id"] + "/" + artifact["sha256"], binary=True)
        if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise ValueError("Source history checksum failed")
        history = MigrationModel.model_validate_json(content)
        # Rebuild locally from our installed templates. Ignore remote programs.
        package = build_migration(history, target, approve_inferred=bool(run.get("inference_review", {}).get("approved")))
        with TemporaryDirectory(prefix="form-cad-worker-") as temp:
            root = Path(temp)
            with ZipFile(BytesIO(package)) as archive:
                for name in archive.namelist():
                    if Path(name).name != name:
                        raise ValueError("Invalid generated package path")
                    (root / name).write_bytes(archive.read(name))
            execute(root, target)
            bundle = result_bundle(root)
        return json.loads(client.request(client.path + "/runs/" + run["id"] + "/result?attempt=" + run["execution"]["attempt_id"], bundle, binary=True))
    except Exception:
        # Do not transmit exception text: runner logs can contain local secrets.
        latest = client.request(client.path)
        client.request(client.path, {"action": "worker-failed", "request_id": str(uuid4()), "expected_revision": latest["revision"], "run_id": run["id"], "attempt_id": run["execution"]["attempt_id"], "note": "Customer CAD worker failed. Inspect local CAD logs before retrying."})
        raise


def fusion_execute(root: Path, target: str):
    """Run the locally regenerated Fusion script on Fusion's main script thread."""
    if target != "fusion360":
        raise ValueError("Fusion worker only accepts Fusion jobs")
    import adsk.core  # Available only in the licensed application.
    if adsk.core.Application.get() is None:
        raise RuntimeError("Run this worker inside Fusion")
    namespace = runpy.run_path(str(root / "rebuild.py"), run_name="form_worker")
    namespace["run"]({})


def run(context):
    """Fusion Scripts entry point; configure environment in the customer session."""
    client = WorkerClient(os.environ["FORMA_CAD_API_URL"], os.environ["FORMA_CAD_TOKEN"], os.environ["FORMA_CAD_PROJECT_ID"])
    return run_once(client, "fusion360", fusion_execute)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--target", choices=("fusion360", "nx", "onshape"), required=True)
    parser.add_argument("--runner", type=Path, required=True, help="Trusted local executable receiving the regenerated package directory")
    args = parser.parse_args()
    runner = args.runner.resolve(strict=True)
    client = WorkerClient(args.api, os.environ["FORMA_CAD_TOKEN"], args.project)
    def execute(root, target):
        subprocess.run([str(runner), str(root), target], check=True, timeout=900, cwd=root)
    run_once(client, args.target, execute)


if __name__ == "__main__":
    main()
