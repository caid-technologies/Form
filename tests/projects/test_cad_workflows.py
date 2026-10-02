"""HTTP and MCP workflows using real SQLite revisions and local artifact storage."""
import asyncio
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import cad_workflows as api
from apps.api.auth import UserContext, require_user_context
from forma_core import database as db
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, project_artifact_storage_key
from forma_core.workspaces.design_briefs import DesignBriefCreate
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ProjectOverview
from forma_core.workspaces.projects.state import ProjectRevisionDraft, ProjectStateError, ProjectStateService
from tests.integrations.test_cad_native_evidence import sample, evidence
from tests.persistence.test_design_briefs import sqlite_repository


class CadWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(sqlite_repository())
        self.storage_dir = self.enterContext(TemporaryDirectory())
        self.enterContext(patch("forma_core.persistence.project_artifacts.get_project_artifact_storage_config", return_value={
            "backend": "local", "enabled": True, "directory": self.storage_dir, "max_bytes": 50*1024*1024}))
        self.project_id, self.owner = str(uuid4()), "cad-owner"
        self.user = UserContext(provider="test", subject=self.owner, owner_user_id=self.owner, is_authenticated=True, is_admin=False)
        self.service = ProjectStateService(db._DATABASE_REPOSITORY)
        self.brief = db.create_design_brief_version(self.project_id, self.owner, DesignBriefCreate(
            schema_version="1.0", conversation_id="cad-test", intent="Build bracket", summary="Build bracket"))
        self.step = b"ISO-10303-21;\n/* synthetic STEP envelope; not real geometry */\nEND-ISO-10303-21;"
        digest = hashlib.sha256(self.step).hexdigest()
        ProjectArtifactStorage().put(self.project_id, digest, self.step, "model/step")
        state = HardwareIntermediateRepresentation(overview=ProjectOverview(title="Bracket", description="Test bracket", difficulty="Beginner", category="Mechanical"),
            cad_model={"format": "step", "stored_sha256": digest})
        self.service.create_initial_revision(ProjectRevisionDraft(state=state), project_id=self.project_id, owner_user_id=self.owner,
            source_job_id="test-initial", design_brief_id=self.brief.design_brief_id, design_brief_version=1)
        app = FastAPI(); app.include_router(api.router)
        app.dependency_overrides[require_user_context] = lambda: self.user
        self.client = self.enterContext(TestClient(app))
        self.url = f"/projects/{self.project_id}/cad-workflows"

    def snapshot(self):
        response = self.client.get(self.url); self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def post(self, action, **kwargs):
        return self.client.post(self.url, json={"request_id": str(uuid4()), "expected_revision": self.snapshot()["revision"], "action": action, **kwargs})

    def plan(self, model=None):
        response = self.post("plan", target="fusion360", history=(model or sample()).model_dump(mode="json"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["runs"][-1]["stale"])
        return response.json()["runs"][-1]

    def test_export_and_exact_revision_download_preserve_geometry_and_metadata(self):
        response = self.post("export", target="solidworks", metadata={"part_number": "BR-001", "revision": "A"})
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json(); run = snapshot["runs"][0]
        self.assertEqual(run["source_revision"], 1); self.assertFalse(run["stale"])
        artifact = run["artifacts"]["package"]
        url = f'{self.url}/artifacts/{snapshot["revision_id"]}/{artifact["sha256"]}'
        downloaded = self.client.get(url)
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), artifact["sha256"])
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            self.assertEqual(archive.read("geometry.step"), self.step)
            metadata = json.loads(archive.read("metadata.json"))
            self.assertEqual(metadata["part_number"], "BR-001")
            self.assertEqual(metadata["source_project_id"], self.project_id)
            self.assertEqual(metadata["source_revision_id"], run["source_revision_id"])
        self.assertEqual(self.client.get(f'{self.url}/artifacts/{snapshot["revision_id"]}/{"1"*64}').status_code, 404)

    def test_full_plan_build_report_review_and_replay(self):
        model = sample(); run = self.plan(model)
        built = self.post("build", run_id=run["id"])
        self.assertEqual(built.status_code, 200, built.text)
        self.assertEqual(built.json()["runs"][0]["status"], "awaiting_native_execution")
        response = self.client.post(self.url + "/runs/" + run["id"] + "/result", content=self.bundle(model))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["runs"][0]["status"], "checks_passed")
        payload = {"request_id": str(uuid4()), "expected_revision": response.json()["revision"], "action": "review",
                   "run_id": run["id"], "decision": "accept", "note": "Synthetic test only; no engineering acceptance."}
        first = self.client.post(self.url, json=payload)
        self.assertEqual(first.status_code, 200, first.text)
        replay = self.client.post(self.url, json=payload)
        self.assertEqual(replay.json(), first.json())
        accepted = replay.json()["runs"][0]
        self.assertEqual(accepted["status"], "accepted_by_reviewer")
        self.assertFalse(accepted["comparison"]["geometry_equivalence_verified"])
        self.assertEqual(accepted["reviews"][0]["evidence_sha256"], accepted["artifacts"]["evidence"]["sha256"])
        payload["note"] = "Changed note with same request ID"
        self.assertEqual(self.client.post(self.url, json=payload).status_code, 409)
        uploaded = self.post("evidence", run_id=run["id"], evidence=evidence(model).model_dump(mode="json"))
        self.assertEqual(uploaded.json()["runs"][0]["reviews"], [])
        old = db.get_project_revision_by_id(self.project_id, self.owner, first.json()["revision_id"])
        self.assertEqual(api._runs(old)[0]["status"], "accepted_by_reviewer")

    def test_inferred_features_need_explicit_note_and_unsupported_cannot_be_overridden(self):
        model = sample(); model.features[0].provenance.method = "ai_inferred"
        run = self.plan(model)
        self.assertEqual(self.post("build", run_id=run["id"]).status_code, 422)
        self.assertEqual(self.post("build", run_id=run["id"], approve_inferred=True).status_code, 422)
        self.assertEqual(self.post("build", run_id=run["id"], approve_inferred=True, note="Compared source dimensions.").status_code, 200)
        model.features[1].kind = "unsupported"
        run = self.plan(model)
        self.assertEqual(self.post("build", run_id=run["id"], approve_inferred=True, note="Review").status_code, 422)

    def test_cannot_accept_before_report_or_with_wrong_history_or_failed_checks(self):
        model = sample(); run = self.plan(model)
        self.assertEqual(self.post("review", run_id=run["id"], decision="accept", note="No report").status_code, 422)
        self.assertEqual(self.post("evidence", run_id=run["id"], evidence=evidence(model).model_dump()).status_code, 422)
        self.post("build", run_id=run["id"])
        wrong = evidence(model).model_dump(); wrong["source_sha256"] = "1"*64
        self.assertEqual(self.post("evidence", run_id=run["id"], evidence=wrong).status_code, 422)
        wrong = evidence(model).model_dump(); wrong["parameter_checks"] = {}
        self.assertEqual(self.post("evidence", run_id=run["id"], evidence=wrong).json()["runs"][0]["status"], "needs_repair")
        self.assertEqual(self.post("review", run_id=run["id"], decision="accept", note="Override").status_code, 422)

    def test_design_edits_keep_audit_artifacts_and_mark_old_runs_stale(self):
        run = self.plan()
        old = self.snapshot()
        state = self.service.get_latest(self.project_id, self.owner).state.model_copy(deep=True)
        state.overview.description = "Changed design"
        state.assembly_metadata.pop("cad_workflows")
        db.append_project_revision(self.project_id, self.owner, state, source_job_id="design-edit")
        current = self.snapshot()
        self.assertTrue(current["runs"][0]["stale"])
        self.assertEqual(self.post("build", run_id=run["id"]).status_code, 409)
        self.assertEqual(self.client.get(f'{self.url}/artifacts/{old["revision_id"]}/{run["artifacts"]["history"]["sha256"]}').status_code, 200)
        self.assertTrue(any(item.kind == "cad-workflow" for item in self.service.get_latest(self.project_id, self.owner).artifacts))

    def test_stale_revision_and_race_do_not_overwrite(self):
        self.plan()
        self.assertEqual(self.client.post(self.url, json={"action": "export", "request_id": str(uuid4()), "expected_revision": 1, "target": "onshape"}).status_code, 409)
        parent = self.service.get_latest(self.project_id, self.owner)
        state = parent.state.model_copy(deep=True)
        db.append_project_revision(self.project_id, self.owner, state, source_job_id="concurrent-edit")
        with self.assertRaises(ProjectStateError):
            db.append_project_revision(self.project_id, self.owner, state, source_job_id="stale-action", expected_parent_revision=parent.revision)

    def test_other_owner_deleted_and_unknown_projects_are_hidden(self):
        snapshot = self.post("export", target="onshape").json()
        digest = snapshot["runs"][0]["artifacts"]["package"]["sha256"]
        self.user = replace(self.user, owner_user_id="different-owner")
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(f'{self.url}/artifacts/{snapshot["revision_id"]}/{digest}').status_code, 404)
        self.assertEqual(self.client.post(self.url, json={"action": "export", "request_id": str(uuid4()), "expected_revision": 2, "target": "onshape"}).status_code, 404)
        self.user = replace(self.user, owner_user_id=self.owner)
        with patch.object(db, "get_project_identity", return_value={"owner_user_id": self.owner, "status": "deletion_pending"}):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_tampered_artifact_oversized_and_invalid_requests(self):
        run = self.plan(); snapshot = self.snapshot()
        digest = run["artifacts"]["history"]["sha256"]
        path = Path(self.storage_dir) / project_artifact_storage_key(self.project_id, digest)
        path.write_bytes(b"tampered")
        self.assertEqual(self.client.get(f'{self.url}/artifacts/{snapshot["revision_id"]}/{digest}').status_code, 503)
        self.assertEqual(self.client.post(self.url, content=b"x"*(api.MAX_BODY+1)).status_code, 413)
        self.assertEqual(self.client.post(self.url, content=b"{}").status_code, 422)
        self.assertEqual(self.post("export", target="nx").status_code, 422)

    def test_mcp_uses_the_same_owner_and_revision_checks(self):
        from apps.api.a2a import _call_mcp_tool, _mcp_tools
        names = {item["name"] for item in _mcp_tools()}
        self.assertIn("forma.cad_workflows", names)
        self.assertIn("forma.cad_capabilities", names)
        result = asyncio.run(_call_mcp_tool("forma.cad_workflows", {"project_id": self.project_id}, self.user))
        self.assertEqual(result["revision"], 1)
        args = {"project_id": self.project_id, "command": {"action": "export", "target": "onshape", "request_id": str(uuid4()), "expected_revision": 1}}
        result = asyncio.run(_call_mcp_tool("forma.cad_workflows", args, self.user))
        self.assertEqual(result["runs"][0]["status"], "package_created")
        self.assertEqual(result["revision"], 2)


    def bundle(self, model, override=None):
        native = b"synthetic native F3D test only"
        files = {"rebuilt.f3d": native, "rebuilt.step": self.step}
        report = evidence(model)
        report.native_artifacts = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
        payload = {"evidence.json": report.model_dump_json().encode(), **files}
        if override: payload.update(override)
        output = io.BytesIO()
        with ZipFile(output, "w") as archive:
            for name, content in payload.items(): archive.writestr(name, content)
        return output.getvalue()

    def test_original_import_is_private_immutable_hash_bound_and_replay_safe(self):
        original = b"synthetic inventor original"
        request = str(uuid4())
        query = {"filename": "source.ipt", "system": "inventor", "expected_revision": 1, "request_id": request}
        response = self.client.post(self.url + "/source", params=query, content=original)
        self.assertEqual(response.status_code, 200, response.text)
        snapshot = response.json()
        source = snapshot["source"]
        self.assertEqual(source["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.client.post(self.url + "/source", params=query, content=original).json(), snapshot)
        self.assertEqual(self.client.post(self.url + "/source", params=query, content=b"changed").status_code, 409)
        self.assertEqual(self.client.get(f'{self.url}/artifacts/{snapshot["revision_id"]}/{source["sha256"]}').content, original)
        model = sample()
        self.assertEqual(self.post("plan", target="fusion360", history=model.model_dump(mode="json")).status_code, 422)
        model.source.sha256 = source["sha256"]
        preview = self.client.post(self.url + "/source", content=self.step, params={"filename": "source.step", "system": "inventor", "preview_of": source["sha256"], "request_id": str(uuid4()), "expected_revision": snapshot["revision"]})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["source"]["sha256"], source["sha256"])
        run = self.plan(model)
        self.assertIn("source", run["artifacts"])
        self.assertIn("source_step", run["artifacts"])
        self.user = replace(self.user, owner_user_id="other")
        self.assertEqual(self.client.post(self.url + "/source", params=query, content=original).status_code, 404)

    def test_native_files_required_and_acceptance_adopts_only_the_new_step(self):
        model = sample(); run = self.plan(model)
        self.post("build", run_id=run["id"])
        self.post("evidence", run_id=run["id"], evidence=evidence(model).model_dump(mode="json"))
        self.assertEqual(self.post("review", run_id=run["id"], decision="accept", note="No actual native bytes").status_code, 422)
        response = self.client.post(self.url + "/runs/" + run["id"] + "/result", content=self.bundle(model))
        self.assertEqual(response.status_code, 200, response.text)
        before = response.json()
        accepted = self.post("review", run_id=run["id"], decision="accept", note="Synthetic model test only")
        self.assertEqual(accepted.status_code, 200, accepted.text)
        new = self.service.get_latest(self.project_id, self.owner)
        self.assertEqual(new.state.cad_model["stored_sha256"], before["runs"][0]["artifacts"]["target_step"]["sha256"])
        self.assertFalse(accepted.json()["runs"][0]["stale"])
        self.assertTrue(any(a.kind == "cad-workflow" and a.metadata.get("filename") == "rebuilt.f3d" for a in new.artifacts))

    def test_cancelled_worker_cannot_return_and_duplicate_claim_is_rejected(self):
        model = sample(); run = self.plan(model)
        self.post("build", run_id=run["id"])
        queued = self.post("queue", run_id=run["id"])
        self.assertEqual(queued.status_code, 200, queued.text)
        attempt = queued.json()["runs"][0]["execution"]["attempt_id"]
        claimed = self.post("claim", run_id=run["id"], worker_id="synthetic-worker")
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(self.post("claim", run_id=run["id"]).status_code, 409)
        self.post("cancel", run_id=run["id"])
        response = self.client.post(f'{self.url}/runs/{run["id"]}/result', params={"attempt": attempt}, content=self.bundle(model))
        self.assertEqual(response.status_code, 409)
        queued = self.post("queue", run_id=run["id"])
        self.assertNotEqual(queued.json()["runs"][0]["execution"]["attempt_id"], attempt)

    def test_result_checksum_paths_formats_and_old_design_are_rejected(self):
        model = sample(); run = self.plan(model)
        self.post("build", run_id=run["id"])
        endpoint = self.url + "/runs/" + run["id"] + "/result"
        for replacement in ({"rebuilt.f3d": b"tampered"}, {"../secret": b"x"}, {"extra.py": b"print(1)"}, {"rebuilt.step": b"invalid STEP"}):
            with self.subTest(replacement=replacement):
                response = self.client.post(endpoint, content=self.bundle(model, replacement))
                self.assertEqual(response.status_code, 422, response.text)
        state = self.service.get_latest(self.project_id, self.owner).state.model_copy(deep=True)
        state.overview.description = "Changed source"
        db.append_project_revision(self.project_id, self.owner, state, source_job_id="changed-source")
        self.assertEqual(self.client.post(endpoint, content=self.bundle(model)).status_code, 409)

    def test_installed_pull_worker_executes_local_template_and_returns_files(self):
        from forma_core.cad_migrations.worker import run_once
        model = sample(); run = self.plan(model)
        self.post("build", run_id=run["id"])
        self.post("queue", run_id=run["id"])
        parent = self
        class LocalClient:
            path = parent.url
            def request(self, path, data=None, binary=False):
                response = parent.client.get(path) if data is None else parent.client.post(path, content=data) if binary else parent.client.post(path, json=data)
                parent.assertEqual(response.status_code, 200, response.text)
                return response.content if binary else response.json()
        def executor(root, target):
            program = (root / "rebuild.py").read_text()
            self.assertIn("import adsk.core", program)
            self.assertIn(hashlib.sha256(api._json(model.normalized())).hexdigest(), program)
            with ZipFile(io.BytesIO(self.bundle(model))) as archive:
                (root / "evidence-test.json").write_bytes(archive.read("evidence.json"))
                for name in ("rebuilt.f3d", "rebuilt.step"): (root / name).write_bytes(archive.read(name))
        result = run_once(LocalClient(), "fusion360", executor)
        self.assertEqual(result["runs"][0]["status"], "checks_passed")
        self.assertEqual(self.snapshot()["runs"][0]["status"], "checks_passed")

    def test_mesh_inputs_are_bounded_and_replacement_report_invalidates_native_files(self):
        model = sample(); run = self.plan(model)
        self.post("build", run_id=run["id"])
        endpoint = self.url + "/runs/" + run["id"] + "/result"
        mesh = {"format": "forma-cad-mesh", "units": "mm", "vertices": [0, 0, 0, 1, 0, 0, 0, 1, 0], "faces": [0, 1, 2]}
        def bundle(value):
            data = json.dumps(value).encode()
            with ZipFile(io.BytesIO(self.bundle(model))) as archive:
                files = {name: archive.read(name) for name in archive.namelist()}
            report = json.loads(files["evidence.json"])
            report["native_artifacts"]["target.mesh.json"] = hashlib.sha256(data).hexdigest()
            files.update({"target.mesh.json": data, "evidence.json": json.dumps(report).encode()})
            output = io.BytesIO()
            with ZipFile(output, "w") as archive:
                for name, data in files.items(): archive.writestr(name, data)
            return output.getvalue()
        for invalid in ([], {**mesh, "units": "cm"}, {**mesh, "vertices": [True]*9}, {**mesh, "faces": [0, 1, 9]}, {**mesh, "vertices": [float("nan")]*9}):
            self.assertEqual(self.client.post(endpoint, content=bundle(invalid)).status_code, 422)
        uploaded = self.client.post(endpoint, content=bundle(mesh)).json()
        mesh_artifact = uploaded["runs"][0]["artifacts"]["target_mesh"]
        exact_url = f'{self.url}/artifacts/{uploaded["revision_id"]}/{mesh_artifact["sha256"]}'
        self.assertEqual(self.client.get(exact_url).status_code, 200)
        replacement = self.post("evidence", run_id=run["id"], evidence=evidence(model).model_dump(mode="json"))
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertNotIn("target_mesh", replacement.json()["runs"][0]["artifacts"])
        self.assertEqual(self.post("review", run_id=run["id"], decision="accept", note="Replacement report has no native model").status_code, 422)
        self.assertEqual(self.client.get(exact_url).status_code, 200)

    def test_worker_baseline_must_match_the_preserved_source_step(self):
        model = sample(); report = evidence(model)
        model.source_geometry = None
        run = self.plan(model)
        self.post("build", run_id=run["id"])
        report.rebuild_sha256 = hashlib.sha256(api._json(model.normalized())).hexdigest()
        report.source_geometry = report.geometry
        report.source_step_sha256 = "f"*64
        invalid = self.post("evidence", run_id=run["id"], evidence=report.model_dump(mode="json"))
        self.assertEqual(invalid.status_code, 422, invalid.text)
        report.source_step_sha256 = run["artifacts"]["source_step"]["sha256"]
        valid = self.post("evidence", run_id=run["id"], evidence=report.model_dump(mode="json"))
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json()["runs"][0]["status"], "checks_passed")

    def test_failed_worker_and_preview_import_cannot_change_another_attempt_or_original(self):
        model = sample(); run = self.plan(model)
        self.post("build", run_id=run["id"])
        self.post("queue", run_id=run["id"])
        claimed = self.post("claim", run_id=run["id"])
        attempt = claimed.json()["runs"][0]["execution"]["attempt_id"]
        self.assertEqual(self.post("worker-failed", run_id=run["id"], attempt_id=str(uuid4()), note="Failure").status_code, 409)
        failed = self.post("worker-failed", run_id=run["id"], attempt_id=attempt, note="Synthetic runner failure")
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["runs"][0]["status"], "worker_failed")
        self.post("queue", run_id=run["id"])
        old_result = self.client.post(f'{self.url}/runs/{run["id"]}/result', params={"attempt": attempt}, content=self.bundle(model))
        self.assertEqual(old_result.status_code, 409)
        snapshot = self.snapshot()
        bad_preview = self.client.post(self.url + "/source", content=self.step, params={"filename": "preview.step", "system": "inventor", "preview_of": "a"*64, "request_id": str(uuid4()), "expected_revision": snapshot["revision"]})
        self.assertEqual(bad_preview.status_code, 422)
        self.assertEqual(self.snapshot(), snapshot)


if __name__ == "__main__": unittest.main()
