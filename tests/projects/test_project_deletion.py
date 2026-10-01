from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from apps.api import project_deletion
from forma_core import database
from forma_core.persistence.models import DBGeneratedProject, DBProject, DBProjectContributionSnapshot
from forma_core.persistence.providers import create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository


class ProjectDeletionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        provider = create_sqlite_provider(
            source="project deletion test",
            url=f"sqlite:///{Path(self.directory.name) / 'forma.db'}",
            import_legacy_jobs=False,
        )
        assert provider.session_factory is not None
        provider.initialize()
        self.provider = provider
        self.original_provider = database._DATABASE_PROVIDER
        self.original_repository = database._DATABASE_REPOSITORY
        database._DATABASE_PROVIDER = provider
        database._DATABASE_REPOSITORY = SqlAlchemyRepository(provider.session_factory)
        self.project_id = str(uuid.uuid4())
        self.owner_id = "user-delete-test"
        database.save_generated_project(
            project_id=self.project_id,
            title="Secret customer prototype",
            prompt="Contact alice@example.com with token sk-secret and private details",
            hardware_ir={
                "components": [
                    {
                        "ref_des": "U1",
                        "part_number": "MCU-1",
                        "category": "Microcontroller",
                        "name": "Customer board",
                        "rationale": "private rationale",
                        "pins": [{"pin_id": "1", "name": "A", "pin_type": "Digital"}],
                    },
                    {
                        "ref_des": "S1",
                        "part_number": "SENSOR-1",
                        "category": "Sensor",
                        "name": "Private sensor",
                        "rationale": "private rationale",
                        "pins": [],
                    },
                ],
                "nets": [
                    {
                        "net_id": "N1",
                        "name": "Secret net",
                        "net_type": "Digital",
                        "pins": [{"ref_des": "U1", "pin_id": "1"}, {"ref_des": "S1", "pin_id": "1"}],
                    }
                ],
                "validation": {
                    "critical": [],
                    "warning": [
                        {
                            "severity": "WARNING",
                            "category": "Test",
                            "description": "private",
                            "troubleshooting": "private",
                        }
                    ],
                },
                "assembly_metadata": {"chat_id": "chat-delete-test", "api_key": "secret"},
                "is_valid": True,
            },
            created_at="2026-07-31T00:00:00Z",
            chat_id="chat-delete-test",
            owner_user_id=self.owner_id,
            visibility="private",
        )

    def tearDown(self) -> None:
        database._DATABASE_PROVIDER = self.original_provider
        database._DATABASE_REPOSITORY = self.original_repository
        self.provider.dispose()
        self.directory.cleanup()

    def test_delete_is_immediate_idempotent_and_restorable(self) -> None:
        with patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=2) as cancel_jobs:
            first = project_deletion.request_project_deletion(self.project_id, self.owner_id)
            second = project_deletion.request_project_deletion(self.project_id, self.owner_id)

        self.assertEqual("deletion_pending", first.status)
        self.assertEqual(first.purge_after, second.purge_after)
        self.assertEqual("deletion_pending", database.get_project_identity(self.project_id)["status"])
        self.assertIsNone(database.get_generated_project(self.project_id))
        self.assertEqual([], database.list_generated_projects(self.owner_id))
        self.assertIsNone(database.get_project_chat("chat-delete-test", self.owner_id))
        cancel_jobs.assert_called_once_with(self.project_id)

        restored = project_deletion.restore_project(self.project_id, self.owner_id)
        self.assertEqual("active", restored.status)
        self.assertEqual("active", database.get_project_identity(self.project_id)["status"])
        self.assertIsNotNone(database.get_generated_project(self.project_id))
        self.assertIsNotNone(database.get_project_chat("chat-delete-test", self.owner_id))

    def test_contribution_is_aggregate_only_and_withdrawal_removes_snapshot(self) -> None:
        consent = project_deletion.grant_contribution_consent(
            self.project_id,
            self.owner_id,
            consent_version="2026-07-31",
            permitted_purposes=["product_research", "evaluation"],
        )
        with patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=0):
            project_deletion.request_project_deletion(self.project_id, self.owner_id)

        assert self.provider.session_factory is not None
        with self.provider.session_factory() as session:
            snapshot = session.query(DBProjectContributionSnapshot).one()
            serialized = json.dumps(snapshot.payload_json)
            self.assertEqual(2, snapshot.payload_json["hardware_summary"]["component_count"])
            self.assertEqual([2], snapshot.payload_json["hardware_summary"]["net_connection_counts"])
            for forbidden in ("alice@example.com", "sk-secret", "Secret customer", "Customer board", "api_key"):
                self.assertNotIn(forbidden, serialized)

        withdrawn = project_deletion.withdraw_contribution(self.project_id, self.owner_id)
        self.assertIsNotNone(withdrawn)
        with self.provider.session_factory() as session:
            self.assertEqual(0, session.query(DBProjectContributionSnapshot).count())
        self.assertEqual(str(consent.id), str(withdrawn.id))

    def test_restore_rejects_an_expired_retention_window(self) -> None:
        with patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=0):
            project_deletion.request_project_deletion(self.project_id, self.owner_id)
        database.update_project_deletion_state(
            self.project_id,
            owner_user_id=self.owner_id,
            allowed_statuses=["deletion_pending"],
            updates={"purge_after": "2000-01-01T00:00:00Z"},
        )
        with self.assertRaisesRegex(RuntimeError, "retention window"):
            project_deletion.restore_project(self.project_id, self.owner_id)

    def test_canonical_identity_lifecycle_does_not_require_generated_projection(self) -> None:
        project_id = str(uuid.uuid4())
        database.ensure_project_identity(
            project_id,
            self.owner_id,
            title="Canonical-only project",
            prompt="Created without a legacy projection.",
        )

        with patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=0):
            deleted = project_deletion.request_project_deletion(project_id, self.owner_id)

        self.assertEqual("deletion_pending", deleted.status)
        self.assertEqual("deletion_pending", database.get_project_identity(project_id)["status"])
        self.assertIsNone(database.get_generated_project(project_id, include_deleted=True))

        restored = project_deletion.restore_project(project_id, self.owner_id)
        self.assertEqual("active", restored["status"] if isinstance(restored, dict) else restored.status)

        with (
            patch.object(project_deletion, "delete_project_images", return_value=0),
            patch.object(project_deletion, "delete_project_videos", return_value=0),
            patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=0),
            patch.object(project_deletion.JOB_STORE, "delete_project_jobs", return_value=0),
        ):
            project_deletion.request_project_deletion(project_id, self.owner_id)
            result = project_deletion.purge_project(project_id)

        self.assertEqual("purged", result["status"])
        self.assertIsNone(database.get_project_identity(project_id))

    def test_account_wide_opt_out_blocks_new_contribution_consent(self) -> None:
        database.set_user_model_training_preference(
            self.owner_id,
            allow_model_training=False,
            updated_at="2026-07-31T00:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "account-wide data setting"):
            project_deletion.grant_contribution_consent(
                self.project_id,
                self.owner_id,
                consent_version="2026-07-31",
                permitted_purposes=["product_research"],
            )

    def test_partial_purge_failure_is_recorded_and_retry_completes(self) -> None:
        with patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=0):
            project_deletion.request_project_deletion(self.project_id, self.owner_id)

        with (
            patch.object(project_deletion, "delete_project_images", side_effect=ConnectionError("storage down")),
            patch.object(project_deletion, "delete_project_videos", return_value=0),
            patch.object(project_deletion.JOB_STORE, "delete_project_jobs", return_value=0),
        ):
            with self.assertRaises(ConnectionError):
                project_deletion.purge_project(self.project_id)

        failed = database.get_generated_project(self.project_id, include_deleted=True)
        self.assertEqual("deletion_failed", failed.status)
        self.assertEqual("ConnectionError", failed.deletion_error)

        with (
            patch.object(project_deletion, "delete_project_images", return_value=3),
            patch.object(project_deletion, "delete_project_videos", return_value=2),
            patch.object(project_deletion.JOB_STORE, "delete_project_jobs", return_value=1),
        ):
            result = project_deletion.purge_project(self.project_id)

        self.assertEqual("purged", result["status"])
        self.assertIsNone(database.get_project_identity(self.project_id))
        self.assertIsNone(database.get_generated_project(self.project_id, include_deleted=True))
        latest = database.get_latest_project_deletion_audit(self.project_id)
        self.assertEqual("purge_completed", latest.action)
        self.assertEqual("succeeded", latest.status)

    def _diverge_deletion_records(self, canonical_status: str, started_at: str | None) -> None:
        with self.provider.session_factory() as session, session.begin():
            identity = session.query(DBProject).filter_by(project_id=self.project_id).one()
            identity.status = canonical_status
            identity.purge_started_at = started_at
            identity.purge_after = None
            legacy = session.query(DBGeneratedProject).filter_by(project_id=self.project_id).one()
            legacy.status = "deletion_failed"
            legacy.purge_started_at = "2000-01-02T00:00:00Z"
            legacy.purge_after = "2000-01-01T00:00:00Z"
            legacy.deletion_error = "StorageApiError"

    def test_due_purge_reclaims_canonical_lease_when_legacy_state_disagrees(self) -> None:
        stale_claim = "2000-01-01T00:00:00Z"
        self._diverge_deletion_records("purging", stale_claim)

        def delete_images(project_id):
            identity = database.get_project_identity(project_id)
            legacy = database.get_generated_project(project_id, include_deleted=True)
            self.assertNotEqual(stale_claim, identity["purge_started_at"])
            self.assertEqual(identity["purge_started_at"], legacy.purge_started_at)
            self.assertEqual("purging", legacy.status)
            return 1

        with (
            patch.object(project_deletion, "delete_project_images", side_effect=delete_images) as images,
            patch.object(project_deletion, "delete_project_videos", return_value=0),
            patch.object(project_deletion.JOB_STORE, "delete_project_jobs", return_value=0),
        ):
            results = project_deletion.purge_due_projects()

        self.assertEqual("purged", results[0]["status"])
        images.assert_called_once_with(self.project_id)
        self.assertIsNone(database.get_project_identity(self.project_id))
        self.assertIsNone(database.get_generated_project(self.project_id, include_deleted=True))

    def test_stale_legacy_state_cannot_purge_a_restored_canonical_project(self) -> None:
        self._diverge_deletion_records("active", None)
        with patch.object(project_deletion, "delete_project_images") as images:
            with self.assertRaisesRegex(RuntimeError, "not scheduled for deletion"):
                project_deletion.purge_project(self.project_id)
        images.assert_not_called()
        self.assertEqual("active", database.get_project_identity(self.project_id)["status"])

    def test_stale_legacy_state_cannot_steal_a_current_canonical_purge(self) -> None:
        self._diverge_deletion_records("purging", project_deletion.iso_timestamp())
        with patch.object(project_deletion, "delete_project_images") as images:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                project_deletion.purge_project(self.project_id)
        images.assert_not_called()

    def test_failed_canonical_claim_does_not_fall_back_to_legacy_record(self) -> None:
        self._diverge_deletion_records("purging", "2000-01-01T00:00:00Z")
        claimed = database.update_project_deletion_state(
            self.project_id, owner_user_id=self.owner_id, allowed_statuses=["deletion_failed"],
            updates={"status": "purging"},
        )
        self.assertIsNone(claimed)
        self.assertEqual("deletion_failed", database.get_generated_project(self.project_id, include_deleted=True).status)

    def test_consent_snapshot_is_unlinked_when_purge_succeeds(self) -> None:
        consent = project_deletion.grant_contribution_consent(
            self.project_id,
            self.owner_id,
            consent_version="2026-07-31",
            permitted_purposes=["ai_system_improvement"],
        )
        with patch.object(project_deletion.JOB_STORE, "cancel_project_jobs", return_value=0):
            project_deletion.request_project_deletion(self.project_id, self.owner_id)
        with (
            patch.object(project_deletion, "delete_project_images", return_value=0),
            patch.object(project_deletion, "delete_project_videos", return_value=0),
            patch.object(project_deletion.JOB_STORE, "delete_project_jobs", return_value=0),
        ):
            project_deletion.purge_project(self.project_id)

        self.assertIsNone(database.get_project_contribution_consent(self.project_id, self.owner_id))
        assert self.provider.session_factory is not None
        with self.provider.session_factory() as session:
            snapshot = session.query(DBProjectContributionSnapshot).one()
            self.assertEqual("anonymized", snapshot.contribution_status)
            self.assertIsNotNone(snapshot.anonymized_at)
            self.assertNotEqual(self.project_id, snapshot.source_project_id)
            self.assertNotEqual(str(consent.id), snapshot.consent_record_id)


class ProjectDeletionWriteGuardTests(unittest.TestCase):
    def test_sanitizer_never_copies_arbitrary_free_text(self) -> None:
        payload = project_deletion.sanitize_project_for_contribution(
            {
                "title": "Person Name",
                "prompt": "token secret",
                "hardware_ir": {
                    "components": [{"category": "Sensor", "rationale": "private@example.com"}],
                    "assembly_metadata": {"repository_url": "https://private.example/repo"},
                },
            }
        )
        serialized = json.dumps(payload)
        self.assertNotIn("Person Name", serialized)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("private.example", serialized)


if __name__ == "__main__":
    unittest.main()
