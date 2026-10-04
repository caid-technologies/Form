from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from forma_core.assets.models import AssetScope, AttachRequest, ComponentIdentity, Registration
from forma_core.assets.repository import AssetRepository
from forma_core.assets.service import ComponentAssetLibrary, AssetRecoveryRequired
from forma_core.persistence.project_artifacts import ProjectArtifactStorage, project_artifact_storage_key
from forma_core.persistence.providers.sqlite import create_sqlite_provider

DATA = b"fixture STEP bytes: kernel validation is isolated from storage tests"
VALIDATION = {"status": "validated", "dimensions_mm": [23, 12.2, 29], "limitations": ["fixture validator"]}


def registration(data=DATA, **overrides):
    values = dict(identity={"manufacturer": "TowerPro", "part_number": "SG90", "revision": "reference-v1",
                            "variants": {"control": "positional", "housing": "23x12.2x29"}},
                  aliases=["SG-90"], provenance={"origin": "generated", "source_urls": [],
                  "retrieved_at": datetime.now(timezone.utc), "license": "MIT", "usage_notes": "Fixture"},
                  dimensions_mm=(23, 12.2, 29), representations=[dict(name="original", format="step",
                  media_type="model/step", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))])
    values.update(overrides)
    return Registration.model_validate(values)


def make_library(root, owner="alice", workspace="lab-a"):
    provider = create_sqlite_provider(source="test", url=f"sqlite:///{root}/metadata.db", import_legacy_jobs=False)
    storage = ProjectArtifactStorage({"enabled": True, "backend": "local", "directory": Path(root)/"blobs",
                                      "bucket": "unused", "max_bytes": 1024*1024})
    return ComponentAssetLibrary(AssetRepository(provider), storage, AssetScope(owner_user_id=owner, workspace_id=workspace))


@pytest.fixture
def library(tmp_path):
    with patch("forma_core.assets.service.inspect_step", return_value=VALIDATION):
        yield make_library(tmp_path)


def test_first_source_second_hit_and_real_process_restart(library, tmp_path):
    reg = registration()
    source = Mock(return_value=(reg, {"original": DATA}))
    first = library.resolve(reg.identity, source)
    assert first["status"] == "hit"
    source.assert_called_once()
    # Reinstantiate repository/storage in an independent interpreter; sourcing is forbidden there.
    code = '''import json, sys
from tests.integrations.test_component_assets import make_library, registration
lib=make_library(sys.argv[1])
def forbidden(identity): raise RuntimeError("source called on cache hit")
result=lib.resolve(registration().identity, forbidden)
print(json.dumps({"result":result,"metrics":lib.metrics}))
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, capture_output=True, text=True)
    second = json.loads(result.stdout.splitlines()[-1])
    assert second["result"]["asset"]["version"] == first["asset"]["version"]
    assert second["metrics"]["source_calls"] == second["metrics"]["blob_writes"] == 0


def test_variants_revisions_and_ambiguous_versions_are_not_substituted(library):
    reg = registration()
    a = library.register(reg, {"original": DATA})
    for update in ({"revision": "v2"}, {"variants": {"control": "continuous"}}):
        identity = reg.identity.model_copy(update=update)
        assert library.search(identity)["status"] == "miss"
    changed = registration(DATA+b"v2")
    b = library.register(changed, {"original": DATA+b"v2"})
    assert a.asset_id == b.asset_id and a.version != b.version
    assert library.search(reg.identity)["status"] == "needs_clarification"
    assert library.content(a.asset_id, a.version, "original") == DATA


def test_normalized_identity_and_repeat_registration_deduplicate(library):
    reg = registration()
    a = library.register(reg, {"original": DATA})
    reg.identity.manufacturer = "  TOWERPRO  "
    b = library.register(reg, {"original": DATA})
    assert a.version == b.version
    assert library.metrics["blob_writes"] == 1
    assert len(library.repository.find(library.scope.key, reg.identity.key)) == 1


def test_concurrent_ingestion_uses_one_version_and_one_blob(library):
    reg = registration()
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(lambda _: library.register(reg, {"original": DATA}).version, range(24)))
    assert len(set(versions)) == 1
    assert len(library.repository.find(library.scope.key, reg.identity.key)) == 1
    files = list(library.storage.config["directory"].rglob("*"))
    assert len([p for p in files if p.is_file()]) == 1


def test_private_scope_blocks_inspect_content_and_search(library, tmp_path):
    reg = registration()
    asset = library.register(reg, {"original": DATA})
    for owner, workspace in (("bob", "lab-a"), ("alice", "lab-b")):
        other = make_library(tmp_path, owner, workspace)
        assert other.search(reg.identity)["status"] == "miss"
        with pytest.raises(PermissionError):
            other.content(asset.asset_id, asset.version, "original")


def test_corrupt_and_missing_blobs_require_explicit_repair(library):
    reg = registration()
    asset = library.register(reg, {"original": DATA})
    path = library.storage.config["directory"] / project_artifact_storage_key(library.blob_scope, reg.representations[0].sha256)
    for corrupt in (True, False):
        if corrupt:
            path.write_bytes(b"bad")
        else:
            path.unlink()
        source = Mock()
        assert library.resolve(reg.identity, source)["status"] == "recovery_required"
        source.assert_not_called()
        with pytest.raises(AssetRecoveryRequired):
            library.revalidate(asset.asset_id, asset.version)
        repaired = library.register(reg, {"original": DATA})
        assert repaired.version == asset.version
        assert library.content(asset.asset_id, asset.version, "original") == DATA


def test_provenance_derived_settings_and_pin_placement(library):
    reg = registration()
    mesh = b"mesh fixture"
    reg.representations.append(type(reg.representations[0])(
        name="preview", format="stl", media_type="model/stl", sha256=hashlib.sha256(mesh).hexdigest(),
        size_bytes=len(mesh), derived_from=reg.representations[0].sha256,
        conversion={"engine": "OCCT", "linear_deflection_mm": 0.1}))
    a = library.register(reg, {"original": DATA, "preview": mesh})
    pin = library.pin(AttachRequest(asset_id=a.asset_id, version=a.version, instance_id="servo-1", position_mm=(10,20,30)))
    assert pin["position_mm"] == [10,20,30]
    assert pin["representations"][1]["conversion"]["linear_deflection_mm"] == 0.1
    assert library.inspect(a.asset_id, a.version).registration.provenance.license == "MIT"
    reg.representations[1].conversion["linear_deflection_mm"] = 0.2
    b = library.register(reg, {"original": DATA, "preview": mesh})
    assert b.version != a.version
    assert pin["version"] == a.version
    assert library.metrics["blob_writes"] == 2


def test_unverified_and_approximate_are_never_silent_hits(library):
    reg = registration(approximate=True, limitations=["Envelope only"])
    a = library.register(reg, {"original": DATA})
    assert library.search(reg.identity)["status"] == "needs_clarification"
    assert library.search(reg.identity, allow_approximate=True)["status"] == "hit"
    with pytest.raises(ValueError, match="approximate"):
        library.pin(AttachRequest(asset_id=a.asset_id, version=a.version, instance_id="S1"))
    with patch("forma_core.assets.service.inspect_step", return_value={"status":"unverified"}):
        unverified = library.register(reg, {"original": DATA})
    with pytest.raises(ValueError, match="Revalidate"):
        library.pin(AttachRequest(asset_id=unverified.asset_id, version=unverified.version, instance_id="S1", allow_approximate=True))


def test_failure_never_commits_reusable_metadata(library):
    reg = registration()
    with pytest.raises(ValueError, match="SHA-256"):
        library.register(reg, {"original": b"bad"})
    with patch.object(library.storage, "put", side_effect=RuntimeError("backend down")):
        with pytest.raises(RuntimeError):
            library.register(reg, {"original": DATA})
    assert library.search(reg.identity)["status"] == "miss"
    with patch("forma_core.assets.service.inspect_step", side_effect=ValueError("invalid STEP")):
        with pytest.raises(ValueError):
            library.register(reg, {"original": DATA})
    assert library.search(reg.identity)["status"] == "miss"


def test_native_step_import_rejects_invalid_and_wrong_envelope():
    pytest.importorskip("OCP")
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
    from forma_core.assets.validation import inspect_step
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/"box.step"
        writer = STEPControl_Writer()
        writer.Transfer(BRepPrimAPI_MakeBox(23, 12.2, 29).Shape(), STEPControl_AsIs)
        writer.Write(str(path))
        assert inspect_step(path.read_bytes(), (23,12.2,29), 0.01)["status"] == "validated"
        with pytest.raises(ValueError, match="dimensions"):
            inspect_step(path.read_bytes(), (20,12.2,29), 0.01)
    with pytest.raises(ValueError, match="imported"):
        inspect_step(b"not a STEP", (23,12.2,29), .1)


def test_aliases_match_only_the_same_revision_and_variant(library):
    reg = registration()
    a = library.register(reg, {"original": DATA})
    alias = reg.identity.model_copy(update={"part_number": "sg-90"})
    assert library.search(alias)["asset"]["asset_id"] == a.asset_id
    assert library.search(alias.model_copy(update={"revision": "other"}))["status"] == "miss"
