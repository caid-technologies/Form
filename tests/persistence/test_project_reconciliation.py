from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from forma_core.persistence.models import (
    DBCliProject,
    DBCliProjectRevision,
    DBGeneratedProject,
    DBProject,
    DBProjectRevision,
)
from forma_core.persistence.project_reconciliation import reconcile_sqlite
from forma_core.persistence.providers.sqlite import create_sqlite_provider
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
from forma_core.workspaces.projects.state import ProjectRevision


class ProjectReconciliationTests(unittest.TestCase):
    def test_generated_backfill_is_dry_run_safe_idempotent_and_auditable(self) -> None:
        project_id = str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test reconciliation",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            provider.initialize()
            assert provider.session_factory is not None
            with provider.session_factory() as session, session.begin():
                session.add(DBGeneratedProject(
                    project_id=project_id,
                    chat_id="chat-1",
                    owner_user_id="user-a",
                    creation_channel="hosted",
                    visibility="public",
                    title="Legacy project",
                    prompt="Build a sensor",
                    hardware_ir={},
                    created_at="2026-09-01T00:00:00Z",
                    status="active",
                ))

            dry_run = reconcile_sqlite(provider.session_factory, dry_run=True)
            self.assertEqual(1, dry_run.scanned)
            self.assertEqual(3, dry_run.migrated)
            self.assertEqual("planned", dry_run.records[0].status)
            with provider.session_factory() as session:
                self.assertEqual(0, session.query(DBProject).count())
                self.assertEqual(0, session.query(DBProjectRevision).count())

            applied = reconcile_sqlite(provider.session_factory, dry_run=False)
            self.assertEqual(3, applied.migrated)
            applied_again = reconcile_sqlite(provider.session_factory, dry_run=False)
            self.assertEqual(0, applied_again.migrated)
            self.assertEqual(0, applied_again.repaired)
            with provider.session_factory() as session:
                self.assertEqual(1, session.query(DBGeneratedProject).count())
                self.assertEqual(1, session.query(DBGeneratedProject).filter_by(project_id=project_id).count())
            provider.dispose()

    def test_cli_backfill_rebuilds_all_compatibility_revisions(self) -> None:
        project_id = str(uuid4())
        revision_one = str(uuid4())
        revision_two = str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test reconciliation",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            provider.initialize()
            assert provider.session_factory is not None
            with provider.session_factory() as session, session.begin():
                session.add(DBCliProject(
                    project_id=project_id,
                    workspace_id="workspace-1",
                    owner_user_id="user-a",
                    title="CLI project",
                    current_revision=2,
                    current_revision_id=revision_two,
                    created_at="2026-09-01T00:00:00Z",
                    updated_at="2026-09-01T00:00:00Z",
                ))
                for revision, revision_id in ((1, revision_one), (2, revision_two)):
                    session.add(DBCliProjectRevision(
                        revision_id=revision_id,
                        project_id=project_id,
                        owner_user_id="user-a",
                        revision=revision,
                        parent_revision_id=revision_one if revision == 2 else None,
                        manifest_json={"project_id": project_id, "project_ir": {}},
                        created_at=f"2026-09-01T00:0{revision}:00Z",
                    ))

            report = reconcile_sqlite(provider.session_factory, dry_run=False)
            self.assertEqual(1, report.scanned)
            self.assertEqual(3, report.migrated)
            with provider.session_factory() as session:
                self.assertEqual(2, session.query(DBCliProjectRevision).filter_by(project_id=project_id).count())
                self.assertEqual(2, session.query(DBProjectRevision).filter_by(project_id=project_id).count())
            provider.dispose()

    def test_canonical_only_projects_rebuild_missing_compatibility_projection(self) -> None:
        project_id = uuid4()
        revision_id = uuid4()
        state = HardwareIntermediateRepresentation.model_validate({})
        revision = ProjectRevision(
            state=state,
            components=[],
            systems=[],
            artifacts=[],
            assumptions=[],
            revision_id=revision_id,
            project_id=project_id,
            owner_user_id="user-a",
            revision=1,
            design_brief_id=uuid4(),
            design_brief_version=1,
            source_job_id="test-canonical-source",
            created_at="2026-09-01T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test reconciliation",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            provider.initialize()
            assert provider.session_factory is not None
            with provider.session_factory() as session, session.begin():
                session.add(DBProject(
                    project_id=str(project_id),
                    owner_user_id="user-a",
                    creation_channel="hosted",
                    title="Canonical project",
                    prompt="Canonical prompt",
                    visibility="public",
                    status="active",
                    created_at="2026-09-01T00:00:00Z",
                    updated_at="2026-09-01T00:00:00Z",
                ))
                session.add(DBProjectRevision(
                    id=str(revision_id),
                    project_id=str(project_id),
                    owner_user_id="user-a",
                    revision=1,
                    parent_revision=None,
                    design_brief_id=str(revision.design_brief_id),
                    design_brief_version=1,
                    source_job_id=revision.source_job_id,
                    payload_json=revision.model_dump(mode="json"),
                    created_at="2026-09-01T00:00:00Z",
                ))
            report = reconcile_sqlite(provider.session_factory, dry_run=False)
            self.assertEqual(1, report.scanned)
            with provider.session_factory() as session:
                self.assertEqual(1, session.query(DBGeneratedProject).filter_by(project_id=str(project_id)).count())
            provider.dispose()


if __name__ == "__main__":
    unittest.main()
