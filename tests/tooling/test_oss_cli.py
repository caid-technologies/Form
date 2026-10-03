from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from forma_cli.app import build_parser, cmd_projects_deliver, cmd_projects_publish, cmd_projects_pull, cmd_projects_push, cmd_render
from forma_cli.config import load_linkage
from forma_cli.credentials import CredentialStore
from forma_cli.local import LocalProjectError, build_project, import_project, init_project
from forma_cli.metadata_api import project_metadata
from forma_cli.sdk import (
    CloudProjectRevision,
    DeliveryProjectRef,
    DeliveryReceipt,
    FormaAPIClient,
    ProjectArtifactDownload,
    TokenSet,
)
from forma_core.database import get_generated_project, init_db, save_generated_project
from forma_core.workspaces.projects.manifest import ProjectArtifactReference, ProjectManifest, write_project_manifest


class FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.values.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.values[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.values.pop((service, name), None)


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class BinaryResponse:
    def __init__(self, content: bytes, headers: dict[str, str]) -> None:
        self.content = content
        self.headers = headers

    def __enter__(self) -> "BinaryResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.content


class OssCliTests(unittest.TestCase):
    def test_help_hierarchy_contains_initial_command_groups(self) -> None:
        parser = build_parser()
        self.assertEqual("forma-oss", parser.prog)
        self.assertEqual(
            {
                "login", "logout", "whoami", "init", "build", "import", "metadata", "metadata-api",
                "status", "render", "projects", "project", "printer", "keys", "version", "doctor",
            },
            set(parser._subparsers._group_actions[0].choices),
        )

    def test_init_creates_local_manifest_and_non_secret_linkage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = init_project(temp_dir, title="Sensor")
            root = Path(temp_dir)
            self.assertEqual("forma-project", json.loads((root / "forma-project.json").read_text())["format"])
            linkage = (root / ".forma" / "project.toml").read_text(encoding="utf-8")
            self.assertIn(f'project_id = "{manifest.project_id}"', linkage)
            self.assertNotIn("token", linkage.lower())

    def test_upload_payload_redacts_provider_secrets_recursively(self) -> None:
        manifest = ProjectManifest(
            project_id="local-project",
            project_ir={
                "components": [],
                "assembly_metadata": {"api_key": "do-not-upload", "safe": "yes"},
                "nested": {"authorization": "Bearer secret", "safe": "yes"},
            },
            extra_token="top-level-secret",
        )

        payload = manifest.upload_payload()

        serialized = json.dumps(payload)
        self.assertNotIn("do-not-upload", serialized)
        self.assertNotIn("Bearer secret", serialized)
        self.assertNotIn("top-level-secret", serialized)
        self.assertEqual("yes", payload["project_ir"]["assembly_metadata"]["safe"])

    def test_local_manifest_writer_does_not_persist_provider_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "forma-project.json"
            write_project_manifest(
                path,
                ProjectManifest(project_id="local-project", project_ir={"api_key": "secret-value"}),
            )
            self.assertNotIn("secret-value", path.read_text(encoding="utf-8"))

    def test_render_writes_a_local_dashboard_png(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = ProjectManifest(
                project_id="render-project",
                title="Rendered project",
                project_ir=json.loads(
                    (Path(__file__).resolve().parents[2] / "apps" / "web" / "public" / "examples" / "plant_watering.json")
                    .read_text(encoding="utf-8")
                ),
            )
            write_project_manifest(root / "forma-project.json", manifest)
            args = type(
                "Args",
                (),
                {"path": temp_dir, "output": "render.png", "width": 720, "height": 520, "yaw": 20.0},
            )()

            self.assertEqual(0, cmd_render(args))
            output = root / "render.png"
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)

    def test_simulated_test_tube_build_preserves_a_provided_cad_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project(temp_dir, title="Test tube")
            root = Path(temp_dir)
            (root / "assembled.step").write_text(
                "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
                encoding="ascii",
            )

            manifest = build_project(temp_dir, prompt="test tube", simulation=True)
            ir = manifest.project_ir

            self.assertEqual("Test Tube Monitor", ir["overview"]["title"])
            self.assertEqual("forma-opencad", ir["cad_model"]["adapter"])
            self.assertEqual(str((root / "assembly.step").resolve()), ir["cad_model"]["path"])
            self.assertTrue((root / "assembly.step").is_file())
            self.assertEqual("assembly.step", manifest.artifacts[-1].path)
            saved = get_generated_project(manifest.project_id)
            self.assertIsNotNone(saved)
            self.assertEqual("local-dev-user", saved.owner_user_id)
            self.assertEqual(
                str((root / "assembly.step").resolve()),
                saved.hardware_ir["cad_model"]["path"],
            )

    def test_build_claims_legacy_unowned_project_for_local_ui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = init_project(temp_dir, title="Legacy project")
            root = Path(temp_dir)
            (root / "assembly.step").write_text(
                "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
                encoding="ascii",
            )
            init_db()
            save_generated_project(
                project_id=manifest.project_id,
                title="Legacy project",
                prompt="legacy",
                hardware_ir={"assembly_metadata": {"project_id": manifest.project_id}},
                created_at="2026-08-30T00:00:00Z",
            )

            build_project(temp_dir, prompt="test tube", simulation=True)

            saved = get_generated_project(manifest.project_id)
            self.assertIsNotNone(saved)
            self.assertEqual("local-dev-user", saved.owner_user_id)

    def test_build_without_manual_cad_artifact_leaves_cad_model_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project(temp_dir, title="Generated assembly")

            with patch(
                "forma_core.workspaces.projects.cad_generation._adapter_path",
                side_effect=RuntimeError("native CAD is unavailable for this CLI test"),
            ):
                manifest = build_project(temp_dir, prompt="test tube", simulation=True)

            root = Path(temp_dir)
            self.assertFalse((root / "assembly.step").exists())
            self.assertIsNone(manifest.project_ir["cad_model"])
            self.assertFalse(any(artifact.path == "assembly.step" for artifact in manifest.artifacts))

    def test_import_preserves_native_cad_and_adds_renderable_stl_mesh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source"
            target = Path(temp_dir) / "target"
            init_project(source, title="Imported controller")
            source_manifest = ProjectManifest(
                project_id="03940f0b-0223-4fa3-921e-9ef3026e670f",
                title="Imported controller",
                prompt="mechanical game controller",
                project_ir=json.loads(
                    (Path(__file__).resolve().parents[2] / "apps" / "web" / "public" / "examples" / "plant_watering.json")
                    .read_text(encoding="utf-8")
                ),
            )
            write_project_manifest(source / "forma-project.json", source_manifest)
            (source / "native.step").write_text(
                "ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
                encoding="ascii",
            )
            (source / "preview.stl").write_text(
                "solid preview\n"
                "facet normal 0 0 1\nouter loop\n"
                "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
                "endloop\nendfacet\nendsolid preview\n",
                encoding="ascii",
            )
            raw = json.loads((source / "forma-project.json").read_text(encoding="utf-8"))
            raw["project_ir"]["cad_model"] = {"path": "native.step", "preview_path": "preview.stl"}
            write_project_manifest(source / "forma-project.json", ProjectManifest.from_document(raw))

            imported = import_project(source, destination=target)

            self.assertEqual("03940f0b-0223-4fa3-921e-9ef3026e670f", imported.project_id)
            self.assertTrue((target / "assembly.step").is_file())
            self.assertTrue((target / "cad-preview.stl").is_file())
            self.assertEqual(1, len(imported.project_ir["cad_model"]["meshes"]))
            self.assertEqual("local-dev-user", get_generated_project(imported.project_id).owner_user_id)

    def test_metadata_reports_project_and_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project(temp_dir, title="Metadata project")
            with patch(
                "forma_core.workspaces.projects.cad_generation._adapter_path",
                side_effect=RuntimeError("native CAD is unavailable for this CLI test"),
            ):
                manifest = build_project(temp_dir, prompt="test tube", simulation=True)

            payload = project_metadata(temp_dir)

            self.assertEqual(manifest.project_id, payload["project_id"])
            self.assertTrue(payload["database"]["present"])
            self.assertEqual("local-dev-user", payload["database"]["owner_user_id"])
            self.assertEqual(4, payload["hardware"]["components"])
            self.assertFalse(payload["cad"]["present"])
            self.assertEqual(0, payload["cad"]["meshes"])
            self.assertIsNone(payload["cad"]["step"])
            self.assertEqual([], payload["artifacts"])

    def test_credential_store_uses_keyring_backend_without_exposing_values(self) -> None:
        keyring = FakeKeyring()
        store = CredentialStore(keyring_backend=keyring)
        store.set("provider:openai", "secret-value")

        self.assertEqual("secret-value", store.get("provider:openai"))
        self.assertNotIn("secret-value", repr({"configured": bool(store.get("provider:openai"))}))
        store.delete("provider:openai")
        self.assertIsNone(store.get("provider:openai"))

    def test_sdk_exchange_persists_typed_tokens_in_credential_store(self) -> None:
        keyring = FakeKeyring()
        client = FormaAPIClient(
            base_url="https://api.example.test",
            credential_store=CredentialStore(keyring_backend=keyring),
        )
        with patch(
            "forma_cli.sdk.urlopen",
            return_value=Response(
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            ),
        ):
            tokens = client.exchange_device_code("device")

        self.assertEqual("access", tokens.access_token)
        self.assertEqual("refresh", client._saved_tokens().refresh_token)

    def test_sdk_transfers_binary_project_artifacts_with_integrity_headers(self) -> None:
        keyring = FakeKeyring()
        client = FormaAPIClient(
            base_url="https://api.example.test",
            credential_store=CredentialStore(keyring_backend=keyring),
        )
        client._save_tokens(
            TokenSet(access_token="access", refresh_token="refresh", expires_in=3600)
        )
        content = b"native cad bytes"
        sha256 = hashlib.sha256(content).hexdigest()
        with patch(
            "forma_cli.sdk.urlopen",
            side_effect=[
                Response({
                    "latest_version": "0.3.4",
                    "minimum_supported_version": "0.3.4",
                    "protocol_version": 1,
                    "hardware_ir_version": "0.2",
                }),
                Response({"status": "uploaded", "sha256": sha256}),
                BinaryResponse(
                    content,
                    {
                        "Content-Type": "model/step",
                        "X-Forma-Artifact-SHA256": sha256,
                        "X-Forma-Artifact-Size": str(len(content)),
                    },
                ),
            ],
        ) as urlopen:
            uploaded = client.upload_project_artifact("project/a", "revision-1", sha256, content, "model/step")
            downloaded = client.download_project_artifact("project/a", "revision-1", sha256)

        upload_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(content, upload_request.data)
        self.assertEqual("model/step", upload_request.get_header("Content-type"))
        self.assertEqual("uploaded", uploaded["status"])
        self.assertEqual(content, downloaded.content)
        self.assertEqual(sha256, downloaded.sha256)
        self.assertEqual(len(content), downloaded.size_bytes)

    def test_projects_pull_writes_typed_remote_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project(temp_dir)
            client = FormaAPIClient(base_url="https://api.example.test", credential_store=CredentialStore(
                keyring_backend=FakeKeyring()
            ))
            client.pull_project = lambda _project_id, _revision_id=None: CloudProjectRevision(
                revision_id="revision-1",
                project_id="project-1",
                revision=1,
                manifest={"format": "forma-project", "project_id": "project-1", "project_ir": {"components": []}},
            )  # type: ignore[method-assign]
            args = type("Args", (), {
                "path": temp_dir,
                "project_id": "project-1",
                "revision_id": None,
                "json": False,
                "api_url": None,
            })()
            with patch("forma_cli.app.FormaAPIClient", return_value=client):
                self.assertEqual(0, cmd_projects_pull(args))
            saved = json.loads((Path(temp_dir) / "forma-project.json").read_text(encoding="utf-8"))
            self.assertEqual("project-1", saved["project_id"])

    def test_projects_push_json_keeps_stdout_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = init_project(temp_dir, title="Upload project")
            delivery_id = "delivery-push-1"
            pending = DeliveryReceipt(
                delivery_id=delivery_id,
                status="pending",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                ),
            )
            completed = DeliveryReceipt(
                delivery_id=delivery_id,
                status="complete",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                    visibility="private",
                ),
            )
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            client.push_project = lambda _manifest, **kwargs: pending  # type: ignore[method-assign]
            client.complete_delivery = lambda _delivery_id: completed  # type: ignore[method-assign]
            args = type("Args", (), {
                "path": temp_dir,
                "yes": True,
                "json": True,
                "api_url": None,
                "key": None,
                "visibility": None,
            })()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(0, cmd_projects_push(args))

            payload = json.loads(stdout.getvalue())
            self.assertEqual("revision-1", payload["project"]["revision_id"])
            self.assertEqual("push", payload["operation"])
            self.assertEqual(
                f"https://api.example.test/project/{manifest.project_id}",
                payload["project_url"],
            )
            self.assertIn("This will upload", stderr.getvalue())

    def test_projects_push_uploads_referenced_files_and_records_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = init_project(root, title="Portable project")
            assembly = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
            (root / "assembly.step").write_bytes(assembly)
            source_manifest = ProjectManifest(
                project_id=manifest.project_id,
                title="Portable project",
                project_ir={
                    "cad_model": {
                        "path": str(root / "assembly.step"),
                        "filename": "assembly.step",
                    }
                },
                artifacts=[ProjectArtifactReference(path="assembly.step", media_type="model/step")],
            )
            write_project_manifest(root / "forma-project.json", source_manifest)
            delivery_id = "delivery-push-1"
            pending = DeliveryReceipt(
                delivery_id=delivery_id,
                status="pending",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                ),
            )
            completed = DeliveryReceipt(
                delivery_id=delivery_id,
                status="complete",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                    visibility="private",
                ),
            )
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            uploaded: list[tuple[str, bytes, str]] = []
            client.push_project = lambda _manifest, **kwargs: pending  # type: ignore[method-assign]
            client.complete_delivery = lambda _delivery_id: completed  # type: ignore[method-assign]
            client.upload_project_artifact = lambda _project_id, _revision_id, sha256, content, media_type: (  # type: ignore[method-assign]
                uploaded.append((sha256, content, media_type))
                or {
                    "status": "uploaded",
                    "sha256": sha256,
                    "media_type": media_type,
                    "size_bytes": len(content),
                }
            )
            args = type("Args", (), {"path": temp_dir, "yes": True, "json": True, "api_url": None, "key": None, "visibility": None})()
            stdout = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout):
                self.assertEqual(0, cmd_projects_push(args))

            payload = json.loads(stdout.getvalue())
            self.assertEqual("uploaded", payload["artifacts"][0]["status"])
            self.assertEqual(assembly, uploaded[0][1])
            self.assertEqual("model/step", uploaded[0][2])
            self.assertEqual(
                "assembly.step",
                json.loads((root / "forma-project.json").read_text(encoding="utf-8"))["artifacts"][0]["path"],
            )
            self.assertEqual(
                "assembly.step",
                json.loads((root / "forma-project.json").read_text(encoding="utf-8"))["project_ir"]["cad_model"]["path"],
            )
            linkage = load_linkage(root)
            self.assertEqual(
                {"assembly.step": hashlib.sha256(assembly).hexdigest()},
                json.loads(linkage["artifact_digests"]),
            )

    def test_projects_deliver_records_receipt_and_stable_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = init_project(temp_dir, title="Deliver project")
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            client.deliver_project = lambda _manifest, *, idempotency_key, parent_revision_id=None, visibility=None: DeliveryReceipt(  # type: ignore[method-assign]
                delivery_id="delivery-1",
                status="pending",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                    parent_revision_id=None,
                    visibility=visibility or "private",
                ),
                artifacts=[],
                artifact_summary={"declared": 0, "present": 0},
            )
            client.complete_delivery = lambda _delivery_id: DeliveryReceipt(  # type: ignore[method-assign]
                delivery_id="delivery-1",
                status="complete",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                    parent_revision_id=None,
                    visibility="private",
                ),
                artifacts=[],
                artifact_summary={"declared": 0, "present": 0},
                created_at="2026-01-01T00:00:00Z",
                completed_at="2026-01-01T00:00:00Z",
            )
            args = type("Args", (), {"path": temp_dir, "yes": True, "json": True, "api_url": None, "key": None, "visibility": None})()
            stdout = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout):
                self.assertEqual(0, cmd_projects_deliver(args))

            payload = json.loads(stdout.getvalue())
            self.assertEqual("deliver", payload["operation"])
            self.assertEqual("complete", payload["status"])
            self.assertTrue(payload["idempotency_key"].startswith("deliver:"))
            self.assertEqual("private", payload["project"]["visibility"])
            linkage = load_linkage(Path(temp_dir))
            self.assertEqual("revision-1", linkage["revision_id"])

    def test_projects_deliver_uploads_referenced_artifacts_before_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = init_project(root, title="Deliver artifacts")
            assembly = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
            (root / "assembly.step").write_bytes(assembly)
            source_manifest = ProjectManifest(
                project_id=manifest.project_id,
                title="Deliver artifacts",
                project_ir={
                    "cad_model": {
                        "path": str(root / "assembly.step"),
                        "filename": "assembly.step",
                    }
                },
                artifacts=[ProjectArtifactReference(path="assembly.step", media_type="model/step")],
            )
            write_project_manifest(root / "forma-project.json", source_manifest)
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            uploaded: list[tuple[str, bytes, str]] = []
            client.deliver_project = lambda _manifest, *, idempotency_key, parent_revision_id=None, visibility=None: DeliveryReceipt(  # type: ignore[method-assign]
                delivery_id="delivery-1",
                status="pending",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                    parent_revision_id=None,
                    visibility="private",
                ),
                artifacts=[{"path": "assembly.step"}, ],
                artifact_summary={"declared": 1, "present": 0},
            )
            client.upload_project_artifact = lambda _project_id, _revision_id, sha256, content, media_type: (  # type: ignore[method-assign]
                uploaded.append((sha256, content, media_type))
                or {
                    "status": "uploaded",
                    "sha256": sha256,
                    "media_type": media_type,
                    "size_bytes": len(content),
                }
            )
            client.complete_delivery = lambda _delivery_id: DeliveryReceipt(  # type: ignore[method-assign]
                delivery_id="delivery-1",
                status="complete",
                project=DeliveryProjectRef(
                    project_id=manifest.project_id,
                    revision_id="revision-1",
                    revision=1,
                    parent_revision_id=None,
                    visibility="private",
                ),
                artifacts=[{"path": "assembly.step", "status": "present"}],
                artifact_summary={"declared": 1, "present": 1},
            )
            args = type("Args", (), {"path": temp_dir, "yes": True, "json": True, "api_url": None, "key": None, "visibility": None})()
            stdout = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout):
                self.assertEqual(0, cmd_projects_deliver(args))

            payload = json.loads(stdout.getvalue())
            self.assertEqual("uploaded", payload["artifacts"][0]["status"])
            self.assertEqual(assembly, uploaded[0][1])
            self.assertEqual("model/step", uploaded[0][2])

    def test_projects_publish_reports_the_explicit_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            init_project(temp_dir)
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            client.publish_project = lambda _project_id: {  # type: ignore[method-assign]
                "project_id": "project-1",
                "visibility": "public",
                "published": True,
                "visibility_before": "private",
                "published_at": "2026-01-01T00:00:00Z",
            }
            args = type("Args", (), {"path": temp_dir, "project_id": None, "json": True, "api_url": None})()
            stdout = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout):
                self.assertEqual(0, cmd_projects_publish(args))

            payload = json.loads(stdout.getvalue())
            self.assertEqual("publish", payload["operation"])
            self.assertEqual("public", payload["visibility"])
            self.assertTrue(payload["published"])

    def test_projects_pull_restores_files_and_rewrites_cad_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            init_project(root)
            content = b"native cad bytes"
            sha256 = hashlib.sha256(content).hexdigest()
            remote_manifest = {
                "format": "forma-project",
                "project_id": "project-1",
                "title": "Remote CAD",
                "project_ir": {
                    "cad_model": {"path": "assembly.step", "filename": "assembly.step"}
                },
                "artifacts": [
                    {
                        "path": "assembly.step",
                        "sha256": sha256,
                        "media_type": "model/step",
                        "size_bytes": len(content),
                    }
                ],
            }
            revision = CloudProjectRevision(
                revision_id="revision-1",
                project_id="project-1",
                revision=1,
                manifest=remote_manifest,
            )
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            client.pull_project = lambda _project_id, _revision_id=None: revision  # type: ignore[method-assign]
            client.download_project_artifact = lambda *_args: ProjectArtifactDownload(  # type: ignore[method-assign]
                content=content,
                content_type="model/step",
                sha256=sha256,
                size_bytes=len(content),
            )
            args = type("Args", (), {
                "path": temp_dir,
                "project_id": "project-1",
                "revision_id": None,
                "json": True,
                "api_url": None,
            })()
            stdout = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout):
                self.assertEqual(0, cmd_projects_pull(args))

            self.assertEqual(content, (root / "assembly.step").read_bytes())
            saved = json.loads((root / "forma-project.json").read_text(encoding="utf-8"))
            self.assertEqual(str((root / "assembly.step").resolve()), saved["project_ir"]["cad_model"]["path"])
            self.assertEqual("assembly.step", saved["project_ir"]["cad_model"]["filename"])
            self.assertEqual("restored", json.loads(stdout.getvalue())["artifacts"][0]["status"])

            stdout = io.StringIO()
            with patch("forma_cli.app.FormaAPIClient", return_value=client), redirect_stdout(stdout):
                self.assertEqual(0, cmd_projects_pull(args))
            self.assertEqual("already_present", json.loads(stdout.getvalue())["artifacts"][0]["status"])

    def test_projects_pull_refuses_to_overwrite_a_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            init_project(root)
            original = b"original"
            original_sha256 = hashlib.sha256(original).hexdigest()
            remote = ProjectManifest(
                project_id="project-1",
                title="Remote",
                project_ir={"cad_model": {"path": "assembly.step"}},
                artifacts=[
                    ProjectArtifactReference(
                        path="assembly.step",
                        sha256=original_sha256,
                        media_type="model/step",
                        size_bytes=len(original),
                    )
                ],
            )
            write_project_manifest(root / "forma-project.json", remote)
            # Simulate a previously pulled revision and then a local edit.
            from forma_cli.config import save_linkage

            save_linkage(
                root,
                {
                    "version": 1,
                    "project_id": "project-1",
                    "remote_project_id": "project-1",
                    "revision_id": "revision-1",
                    "manifest_digest": hashlib.sha256(
                        json.dumps(remote.upload_payload(), sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "artifact_digests": json.dumps({"assembly.step": original_sha256}),
                },
            )
            (root / "assembly.step").write_bytes(b"local edit")
            revision = CloudProjectRevision(
                revision_id="revision-1",
                project_id="project-1",
                revision=1,
                manifest=remote.upload_payload(),
            )
            client = FormaAPIClient(
                base_url="https://api.example.test",
                credential_store=CredentialStore(keyring_backend=FakeKeyring()),
            )
            client.pull_project = lambda _project_id, _revision_id=None: revision  # type: ignore[method-assign]
            args = type("Args", (), {
                "path": temp_dir,
                "project_id": "project-1",
                "revision_id": None,
                "json": False,
                "api_url": None,
            })()
            with patch("forma_cli.app.FormaAPIClient", return_value=client):
                with self.assertRaisesRegex(LocalProjectError, "refusing to overwrite local changes"):
                    cmd_projects_pull(args)
            self.assertEqual(b"local edit", (root / "assembly.step").read_bytes())


if __name__ == "__main__":
    unittest.main()
