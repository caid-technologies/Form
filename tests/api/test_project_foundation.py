from io import BytesIO
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import zipfile
import pytest
from pathlib import Path
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.auth import UserContext, require_user_context
from apps.api import project_foundation_api as routes
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation


def test_stage_outputs_and_canonical_assumptions_survive_portable_export():
    from apps.api.project_foundation import revision_project
    from forma_core.workspaces.projects.inspection import inspect_project, record_project_review
    from forma_core.workspaces.projects.portable import export_project_bundle, import_project_bundle, local_artifact_reader
    from forma_core.workspaces.projects.state import ProjectArtifact
    project_id = str(uuid4())
    output = {"requirements": ["Inspect exported design"], "api_key": "private-fixture"}
    checksum = "sha256:" + hashlib.sha256(json.dumps(output, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = ProjectArtifact(artifact_id="run:requirements:attempt:1", kind="generation-stage:requirements",
                               uri="forma://generation-runs/run/stages/requirements/attempts/1",
                               checksum=checksum, media_type="application/json")
    state = HardwareIntermediateRepresentation(assembly_metadata={"generation_run": {"records": {
        "requirements": {"artifact_id": artifact.artifact_id, "artifact": artifact.model_dump(mode="json"),
                         "output": output, "output_fingerprint": checksum}}}})
    revision = SimpleNamespace(revision_id=uuid4(), state=state, artifacts=[artifact],
                               assumptions=["Dimensions must be confirmed against supplied components."])
    manifest, reader = revision_project(revision, project_id, "owner")
    report = inspect_project(manifest, read_artifact=reader)
    assert report.is_valid
    assert report.assumptions == revision.assumptions
    assert json.loads(reader(manifest.artifacts[0])) == {"requirements": ["Inspect exported design"]}
    assert manifest.artifacts[0].provenance["source_checksum"] == checksum
    assert "generation_run" not in manifest.project_ir["assembly_metadata"]
    assert manifest.project_ir["assembly_metadata"]["generation_history"]["execution_authority"] == "historical_only"
    assert state.assembly_metadata["generation_run"]["records"]  # Canonical state is unchanged.
    reviewed = record_project_review(manifest, decision="approved", reviewer="Engineer")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bundle = export_project_bundle(reviewed, root / "project.zip", read_artifact=reader)
        restored = import_project_bundle(bundle, root / "restored")
        reopened = inspect_project(restored, read_artifact=local_artifact_reader(root / "restored"))
        assert reopened.human_review.status == "approved"
        assert "generation_run" not in restored.project_ir["assembly_metadata"]
        assert restored.project_ir["assembly_metadata"]["generation_history"]["run"]["records"]
    state.assembly_metadata["generation_run"]["records"]["requirements"]["output"]["requirements"] = ["tampered"]
    with pytest.raises(ValueError, match="checksum"):
        revision_project(revision, project_id, "owner")


def test_owner_application_and_mcp_inspect_the_same_exact_revision():
    project_id, revision_id = uuid4(), uuid4()
    revision = SimpleNamespace(revision_id=revision_id, state=HardwareIntermediateRepresentation(), artifacts=[])
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_user_context] = lambda: UserContext(
        provider="clerk", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
    with patch.object(routes, "get_project_identity", return_value={"owner_user_id": "owner"}), \
         patch.object(routes, "get_project_revision_by_id", return_value=revision) as read_revision:
        client = TestClient(app)
        query = f"/projects/{project_id}/foundation"
        inspected = client.get(f"{query}/inspection?revision_id={revision_id}")
        assert inspected.status_code == 200
        assert inspected.headers["cache-control"] == "private, no-store"
        assert inspected.json()["human_review"]["status"] == "pending"
        assert inspected.json()["project_readiness"] == "draft"
        bundle = client.get(f"{query}/bundle?revision_id={revision_id}")
        assert bundle.status_code == 200
        with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
            report = json.loads(archive.read("forma-inspection.json"))
            assert report["manifest_sha256"] == inspected.json()["manifest_sha256"]
        read_revision.assert_called_with(str(project_id), "owner", str(revision_id))
        app.dependency_overrides[require_user_context] = lambda: UserContext(
            provider="clerk", subject="other", owner_user_id="other", is_authenticated=True, is_admin=False)
        assert client.get(f"{query}/inspection").status_code == 404
        assert client.get(f"{query}/bundle").status_code == 404


def test_application_export_does_not_publish_corrupt_artifacts():
    project_id = uuid4()
    from forma_core.workspaces.projects.state import ProjectArtifact
    revision = SimpleNamespace(revision_id=uuid4(), state=HardwareIntermediateRepresentation(), artifacts=[
        ProjectArtifact(artifact_id="missing", kind="file", uri="private", checksum="0" * 64, media_type="model/step")])
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_user_context] = lambda: UserContext(
        provider="clerk", subject="owner", owner_user_id="owner", is_authenticated=True, is_admin=False)
    with patch.object(routes, "get_project_identity", return_value={"owner_user_id": "owner"}), \
         patch.object(routes, "get_latest_project_revision", return_value=revision), \
         patch("forma_core.persistence.project_artifacts.ProjectArtifactStorage.get", side_effect=ValueError("corrupt")):
        assert TestClient(app).get(f"/projects/{project_id}/foundation/bundle").status_code == 409


def test_application_bundle_redacts_virtual_snapshots_and_rewrites_machine_paths():
    from forma_core.persistence.project_artifacts import StoredProjectArtifact
    from forma_core.workspaces.projects.state import ProjectArtifact

    project_id=uuid4()
    source=b"# inspectable source fixture\n"
    digest=hashlib.sha256(source).hexdigest()
    machine_path="/private/other-machine/cad-source.py"
    sentinel="fixture-credential-must-not-be-exported"
    state=HardwareIntermediateRepresentation(assembly_metadata={"api_key":sentinel,"nested":{"password":sentinel}},
        cad_model={"model_source_path":machine_path,"source_artifacts":{"model_source":{"sha256":digest}}})
    revision=SimpleNamespace(revision_id=uuid4(),state=state,artifacts=[
        ProjectArtifact(artifact_id="project-state",kind="project-state",uri="internal",media_type="application/vnd.forma.hardware-ir+json"),
        ProjectArtifact(artifact_id="source",kind="cad.source",uri="private",checksum="sha256:"+digest,media_type="text/x-python"),
    ])
    app=FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_user_context]=lambda:UserContext(
        provider="clerk",subject="owner",owner_user_id="owner",is_authenticated=True,is_admin=False)
    with patch.object(routes,"get_project_identity",return_value={"owner_user_id":"owner"}), \
         patch.object(routes,"get_latest_project_revision",return_value=revision), \
         patch("forma_core.persistence.project_artifacts.ProjectArtifactStorage.get",
               return_value=StoredProjectArtifact(str(project_id),digest,"text/x-python",len(source),source)):
        response=TestClient(app).get(f"/projects/{project_id}/foundation/bundle")
    assert response.status_code==200,response.text
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        for name in archive.namelist():
            content=archive.read(name)
            assert sentinel.encode() not in content,name
            assert machine_path.encode() not in content,name
        manifest=json.loads(archive.read("forma-project.json"))
        assert manifest["project_ir"]["cad_model"]["model_source_path"]=="artifacts/0001.py"
        snapshot=json.loads(archive.read("artifacts/0000.json"))
        assert snapshot["cad_model"]["model_source_path"]=="artifacts/0001.py"
