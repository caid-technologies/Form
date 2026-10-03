from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from forma_core.jobs.store import JobMetadataStore
from forma_core import database
from forma_core.persistence import APPLICATION_SCHEMA
from forma_core.persistence.models import DBGeneratedProject, DBProject, DBProjectRevision
from forma_core.jobs.migrations import import_legacy_job_database
from forma_core.persistence.providers import SupabaseProvider, create_sqlite_provider
from forma_core.persistence.repositories import SqlAlchemyRepository, SupabaseRepository
from forma_core.workspaces.design_briefs import DESIGN_BRIEF_SCHEMA_VERSION, DesignBrief
from forma_core.workspaces.projects import ProjectRevision
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation, ProjectOverview


class PersistenceArchitectureTests(unittest.TestCase):
    def test_disposing_sqlite_provider_releases_database_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "forma.db"
            provider = create_sqlite_provider(
                source="test provider lifecycle",
                url=f"sqlite:///{database_path}",
                import_legacy_jobs=False,
            )
            provider.initialize()
            provider.dispose()

            database_path.unlink()

        self.assertFalse(database_path.exists())

    def test_canonical_revision_publication_creates_public_gallery_projection_without_replacing_chat(self) -> None:
        project_id = uuid.uuid4()
        design_brief_id = uuid.uuid4()
        brief = DesignBrief(
            schema_version=DESIGN_BRIEF_SCHEMA_VERSION,
            conversation_id="conversation-publication",
            intent="Build a sensor",
            summary="Build a compact sensor controller.",
            requirements=["Read a sensor"],
            constraints=["Use low voltage"],
            readiness="ready",
            design_brief_id=design_brief_id,
            project_id=project_id,
            brief_version=1,
            created_at=datetime.now(timezone.utc),
        )
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Published Sensor",
                description="A compact sensor controller.",
                difficulty="Beginner",
                category="Sensors",
            ),
            assembly_metadata={"project_id": str(project_id)},
        )
        revision = ProjectRevision(
            revision_id=uuid.uuid4(),
            project_id=project_id,
            owner_user_id="publication-owner",
            revision=1,
            design_brief_id=design_brief_id,
            design_brief_version=1,
            source_job_id="publication-job",
            state=state,
            created_at=datetime.now(timezone.utc),
        )

        with patch.object(database, "get_generated_project", return_value=None), patch.object(
            database,
            "save_generated_project",
        ) as save_project:
            published_id = database.publish_project_revision(revision, brief, "publication-owner")

        self.assertEqual(str(project_id), published_id)
        save_project.assert_called_once()
        saved = save_project.call_args.kwargs
        self.assertEqual("public", saved["visibility"])
        self.assertFalse(saved["create_chat_record"])
        self.assertEqual("conversation-publication", saved["chat_id"])
        self.assertEqual("conversation-publication", saved["hardware_ir"]["assembly_metadata"]["chat_id"])

    def test_legacy_project_iteration_bootstraps_one_canonical_revision_then_appends(self) -> None:
        project_id = str(uuid.uuid4())
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Legacy Sensor",
                description="A legacy sensor.",
                difficulty="Beginner",
                category="Sensors",
            ),
            assembly_metadata={"project_id": project_id},
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test legacy migration",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            original_repository = database._DATABASE_REPOSITORY
            try:
                database._DATABASE_REPOSITORY = repository
                database.save_generated_project(
                    project_id=project_id,
                    title="Legacy Sensor",
                    prompt="Build a legacy sensor.",
                    hardware_ir=state.model_dump(mode="json"),
                    created_at="2026-08-08T12:00:00Z",
                    chat_id="legacy-chat",
                    owner_user_id="legacy-owner",
                    visibility="private",
                )

                revised = state.model_copy(deep=True)
                revised.overview.description = "Revised legacy sensor."
                first = database.append_project_revision(
                    project_id,
                    "legacy-owner",
                    revised,
                    source_job_id="iteration-legacy-1",
                )
                replay = database.append_project_revision(
                    project_id,
                    "legacy-owner",
                    revised,
                    source_job_id="iteration-legacy-1",
                )
                latest = database.get_latest_project_revision(project_id, "legacy-owner")
                briefs = database.list_design_brief_versions(project_id, "legacy-owner")
            finally:
                database._DATABASE_REPOSITORY = original_repository
                provider.dispose()

        self.assertEqual(2, first.revision)
        self.assertEqual(1, first.parent_revision)
        self.assertEqual(first.revision_id, replay.revision_id)
        self.assertEqual(2, latest.revision)
        self.assertEqual(1, len(briefs))

    def test_supabase_provider_checks_complete_schema_contract(self) -> None:
        client = _SchemaClient()
        provider = SupabaseProvider(source="test", url="https://example.supabase.co", client=client)

        provider.initialize()

        self.assertEqual([table.name for table in APPLICATION_SCHEMA], list(client.projections))
        self.assertIn("error_debug_json", client.projections["a2a_jobs"])
        self.assertIn("visibility", client.projections["generated_projects"])
        self.assertIn("model_training_opt_out", client.projections["user_settings"])
        self.assertIn("remix_project_id", client.projections["project_remixes"])
        self.assertIn("project_id", client.projections["project_saves"])

    def test_supabase_client_uses_http1_transport_for_concurrent_sync_requests(self) -> None:
        with patch("supabase.create_client") as create_client, patch("httpx.Client") as http_client:
            database._build_supabase_client("https://example.supabase.co", "secret")

        http_client.assert_called_once_with(http2=False, follow_redirects=True, timeout=120)
        options = create_client.call_args.kwargs["options"]
        self.assertIs(options.httpx_client, http_client.return_value)

    def test_supabase_provider_propagates_original_readiness_error(self) -> None:
        failure = ConnectionError("[Errno 111] Connection refused")
        client = _SchemaClient(
            failing_table="component_templates",
            failure=failure,
        )
        provider = SupabaseProvider(source="test", url="http://127.0.0.1:54321", client=client)

        with self.assertRaises(ConnectionError) as raised:
            provider.initialize()

        self.assertIs(raised.exception, failure)

    def test_default_job_store_uses_primary_database_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            original_provider = database._DATABASE_PROVIDER
            original_repository = database._DATABASE_REPOSITORY
            try:
                database._DATABASE_PROVIDER = provider
                database._DATABASE_REPOSITORY = SqlAlchemyRepository(provider.session_factory)
                provider.initialize()
                store = JobMetadataStore(backend="sqlite")
                job_id = f"job_shared_{uuid.uuid4().hex}"

                store.create_job(
                    job_id=job_id,
                    message_id=f"msg_{uuid.uuid4().hex}",
                    correlation_id=None,
                    action="forma.generate_project",
                    sender="test",
                    recipient="forma",
                    payload={"prompt": "shared database"},
                    server_owned=True,
                )

                assert provider.engine is not None
                with provider.engine.connect() as connection:
                    stored_job_id = connection.exec_driver_sql(
                        "SELECT job_id FROM a2a_jobs WHERE job_id = ?",
                        (job_id,),
                    ).scalar_one()
            finally:
                database._DATABASE_PROVIDER = original_provider
                database._DATABASE_REPOSITORY = original_repository
                provider.dispose()

        self.assertEqual(job_id, stored_job_id)
        self.assertEqual("primary", store.get_config()["scope"])

    def test_database_facade_delegates_project_and_chat_work_to_selected_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            repository = SqlAlchemyRepository(provider.session_factory)
            provider.initialize()
            original_repository = database._DATABASE_REPOSITORY
            try:
                database._DATABASE_REPOSITORY = repository
                project_id = str(uuid.uuid4())
                database.save_generated_project(
                    project_id=project_id,
                    title="Shared persistence",
                    prompt="exercise repository routing",
                    hardware_ir={"assembly_metadata": {"chat_id": "chat_shared"}},
                    created_at="2026-07-25T12:00:00Z",
                    chat_id="chat_shared",
                    owner_user_id="user_shared",
                    visibility="private",
                )

                project = database.get_generated_project(project_id)
                chat = database.get_project_chat("chat_shared", "user_shared")
                deleted = database.delete_project_chat("chat_shared", "user_shared")
                project_after_chat_delete = database.get_generated_project(project_id)
            finally:
                database._DATABASE_REPOSITORY = original_repository
                provider.dispose()

        self.assertIsNotNone(project)
        self.assertEqual("private", project.visibility)
        self.assertIsNotNone(chat)
        self.assertTrue(deleted)
        self.assertIsNone(project_after_chat_delete.chat_id)

    def test_sqlite_repository_lists_only_each_projects_latest_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            with provider.session_factory() as session, session.begin():
                session.add_all([
                    DBProjectRevision(
                        id="revision-a1",
                        project_id="project-a",
                        owner_user_id="user-a",
                        revision=1,
                        parent_revision=None,
                        design_brief_id="brief-a",
                        design_brief_version=1,
                        source_job_id="job-a1",
                        payload_json={},
                        created_at="2026-08-07T12:00:00Z",
                    ),
                    DBProjectRevision(
                        id="revision-a2",
                        project_id="project-a",
                        owner_user_id="user-a",
                        revision=2,
                        parent_revision=1,
                        design_brief_id="brief-a",
                        design_brief_version=1,
                        source_job_id="job-a2",
                        payload_json={},
                        created_at="2026-08-07T12:02:00Z",
                    ),
                    DBProjectRevision(
                        id="revision-b1",
                        project_id="project-b",
                        owner_user_id="user-a",
                        revision=1,
                        parent_revision=None,
                        design_brief_id="brief-b",
                        design_brief_version=1,
                        source_job_id="job-b1",
                        payload_json={},
                        created_at="2026-08-07T12:01:00Z",
                    ),
                ])

            revisions = repository.list_latest_project_revisions("user-a")
            provider.dispose()

        self.assertEqual(
            [("project-a", 2), ("project-b", 1)],
            [(revision.project_id, revision.revision) for revision in revisions],
        )

    def test_sqlite_gallery_inventory_prefers_identity_and_current_revision_with_legacy_fallback(self) -> None:
        canonical_id = str(uuid.uuid4())
        legacy_id = str(uuid.uuid4())
        deleted_id = str(uuid.uuid4())
        state = HardwareIntermediateRepresentation(
            overview=ProjectOverview(
                title="Current canonical title",
                description="Canonical state",
                difficulty="Beginner",
                category="Automation",
            ),
            assembly_metadata={"project_id": canonical_id},
        )

        def revision_record(revision: int) -> DBProjectRevision:
            return DBProjectRevision(
                id=f"gallery-revision-{revision}",
                project_id=canonical_id,
                owner_user_id="user-a",
                revision=revision,
                parent_revision=revision - 1 if revision > 1 else None,
                design_brief_id="brief-gallery",
                design_brief_version=1,
                source_job_id=f"gallery-job-{revision}",
                payload_json={
                    "schema_version": "1.0",
                    "state": state.model_dump(mode="json"),
                    "components": [],
                    "systems": [],
                    "artifacts": [],
                    "assumptions": [],
                    "revision_id": str(uuid.uuid4()),
                    "project_id": canonical_id,
                    "owner_user_id": "user-a",
                    "revision": revision,
                    "parent_revision": revision - 1 if revision > 1 else None,
                    "design_brief_id": str(uuid.uuid4()),
                    "design_brief_version": 1,
                    "source_job_id": f"gallery-job-{revision}",
                    "created_at": f"2026-08-0{revision}T12:00:00Z",
                },
                created_at=f"2026-08-0{revision}T12:00:00Z",
            )

        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test gallery inventory",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            with provider.session_factory() as session, session.begin():
                session.add_all([
                    DBProject(
                        project_id=canonical_id,
                        owner_user_id="user-a",
                        creation_channel="hosted",
                        title="Identity title",
                        prompt="Canonical prompt",
                        chat_id="canonical-chat",
                        visibility="public",
                        status="active",
                        created_at="2026-08-01T12:00:00Z",
                        updated_at="2026-08-02T12:00:00Z",
                    ),
                    DBProject(
                        project_id=deleted_id,
                        owner_user_id="user-a",
                        creation_channel="hosted",
                        title="Deleted project",
                        prompt="Deleted",
                        visibility="public",
                        status="pending_purge",
                        created_at="2026-08-01T12:00:00Z",
                        updated_at="2026-08-03T12:00:00Z",
                    ),
                    revision_record(1),
                    revision_record(2),
                    DBGeneratedProject(
                        project_id=canonical_id,
                        owner_user_id="user-a",
                        visibility="public",
                        title="Stale generated projection",
                        prompt="Stale",
                        hardware_ir={"assembly_metadata": {"project_id": canonical_id}},
                        created_at="2026-08-04T12:00:00Z",
                        status="active",
                    ),
                    DBGeneratedProject(
                        project_id=legacy_id,
                        owner_user_id="user-a",
                        visibility="public",
                        title="Legacy fallback",
                        prompt="Legacy",
                        hardware_ir={"assembly_metadata": {"project_id": legacy_id}},
                        created_at="2026-08-05T12:00:00Z",
                        status="active",
                    ),
                    DBProject(
                        project_id=legacy_id,
                        owner_user_id="user-a",
                        creation_channel="hosted",
                        title="Legacy identity",
                        prompt="Legacy identity prompt",
                        visibility="public",
                        status="active",
                        created_at="2026-08-05T12:00:00Z",
                        updated_at="2026-08-05T12:00:00Z",
                    ),
                ])

            rows, total = repository.list_project_gallery_inventory_page(
                "user-a", visibility="public", limit=10, offset=0
            )
            provider.dispose()

        self.assertEqual(2, total)
        self.assertEqual(["legacy", "canonical"], [row.source for row in rows])
        canonical = next(row for row in rows if row.project_id == canonical_id)
        self.assertEqual(2, canonical.revision)
        self.assertEqual("Identity title", canonical.title)

    def test_cli_revision_push_updates_project_summary_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            try:
                project_record = {
                    "project_id": "cli-summary",
                    "workspace_id": "workspace-a",
                    "owner_user_id": "user-a",
                    "title": "Initial title",
                    "current_revision": 0,
                    "current_revision_id": None,
                    "created_at": "2026-08-31T12:00:00Z",
                    "updated_at": "2026-08-31T12:00:00Z",
                }
                first_revision = {
                    "revision_id": "cli-summary-r1",
                    "project_id": "cli-summary",
                    "owner_user_id": "user-a",
                    "revision": 1,
                    "parent_revision_id": None,
                    "manifest_json": {},
                    "created_at": "2026-08-31T12:00:00Z",
                }
                self.assertIsNotNone(repository.insert_cli_project_revision(project_record, first_revision, None))

                updated_project_record = {**project_record, "workspace_id": "workspace-b", "title": "Updated title"}
                second_revision = {
                    **first_revision,
                    "revision_id": "cli-summary-r2",
                    "revision": 2,
                    "parent_revision_id": "cli-summary-r1",
                }
                self.assertIsNotNone(
                    repository.insert_cli_project_revision(updated_project_record, second_revision, "cli-summary-r1")
                )

                project = repository.get_cli_project("cli-summary", "user-a")
                identity = repository.get_project_identity("cli-summary")
            finally:
                assert provider.engine is not None
                provider.dispose()

        self.assertIsNotNone(project)
        self.assertEqual("workspace-b", project.workspace_id)
        self.assertEqual("Updated title", project.title)
        self.assertEqual(2, project.current_revision)
        self.assertEqual(2, identity["current_revision"])
        self.assertEqual("cli-summary-r2", identity["current_revision_id"])
        self.assertEqual("public", identity["visibility"])

    def test_uuid_cli_revision_is_also_written_to_canonical_revision_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            project_id = str(uuid.uuid4())
            revision_id = str(uuid.uuid4())
            try:
                saved = repository.insert_cli_project_revision(
                    {
                        "project_id": project_id,
                        "workspace_id": None,
                        "owner_user_id": "user-a",
                        "title": "Canonical CLI project",
                        "current_revision": 0,
                        "current_revision_id": None,
                        "created_at": "2026-08-31T12:00:00Z",
                        "updated_at": "2026-08-31T12:00:00Z",
                    },
                    {
                        "revision_id": revision_id,
                        "project_id": project_id,
                        "owner_user_id": "user-a",
                        "revision": 1,
                        "parent_revision_id": None,
                        "manifest_json": {
                            "project_id": project_id,
                            "project_ir": {},
                        },
                        "created_at": "2026-08-31T12:00:00Z",
                    },
                    None,
                )
                canonical = repository.get_latest_project_revision(project_id, "user-a")
            finally:
                provider.dispose()

        self.assertIsNotNone(saved)
        self.assertIsNotNone(canonical)
        self.assertEqual(revision_id, str(canonical.id))

    def test_sqlite_repository_pages_filtered_projects_before_loading_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            with provider.session_factory() as session, session.begin():
                session.add_all([
                    DBGeneratedProject(
                        project_id=f"project-{index}",
                        owner_user_id="user-a" if index < 8 else "user-b",
                        visibility="public" if index % 2 == 0 else "private",
                        title=f"Project {index}",
                        prompt="Build a test fixture.",
                        hardware_ir={},
                        created_at=f"2026-08-{index + 1:02d}T00:00:00Z",
                        status="active",
                    )
                    for index in range(10)
                ])

            projects, total = repository.list_generated_projects_page(
                "user-a",
                visibility="public",
                limit=2,
                offset=1,
            )
            provider.dispose()

        self.assertEqual(4, total)
        self.assertEqual(["project-4", "project-2"], [project.project_id for project in projects])

    def test_sqlite_repository_searches_project_titles_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            provider.initialize()
            repository = SqlAlchemyRepository(provider.session_factory)
            with provider.session_factory() as session, session.begin():
                session.add_all([
                    DBGeneratedProject(
                        project_id="title-match",
                        visibility="public",
                        title="Compact Motor Controller",
                        prompt="Build a portable drive.",
                        hardware_ir={},
                        created_at="2026-08-03T00:00:00Z",
                        status="active",
                    ),
                    DBGeneratedProject(
                        project_id="prompt-match",
                        visibility="public",
                        title="Factory Fixture",
                        prompt="Build a MOTOR test stand.",
                        hardware_ir={},
                        created_at="2026-08-02T00:00:00Z",
                        status="active",
                    ),
                    DBGeneratedProject(
                        project_id="no-match",
                        visibility="public",
                        title="Weather Station",
                        prompt="Measure temperature.",
                        hardware_ir={},
                        created_at="2026-08-01T00:00:00Z",
                        status="active",
                    ),
                ])

            projects, total = repository.list_generated_projects_page(
                None,
                visibility="public",
                limit=6,
                offset=0,
                search="motor",
            )
            provider.dispose()

        self.assertEqual(2, total)
        self.assertEqual(
            ["title-match", "prompt-match"],
            [project.project_id for project in projects],
        )

    def test_supabase_repository_requests_an_exact_count_and_bounded_range(self) -> None:
        client = _ProjectPageClient()
        repository = SupabaseRepository(client)

        projects, total = repository.list_generated_projects_page(
            "user-a",
            visibility="public",
            limit=6,
            offset=12,
        )

        self.assertEqual(37, total)
        self.assertEqual(["project-13", "project-14"], [project.project_id for project in projects])
        self.assertEqual("exact", client.query.count_mode)
        self.assertEqual((12, 17), client.query.requested_range)
        self.assertIn(("status", "active"), client.query.filters)
        self.assertIn(("owner_user_id", "user-a"), client.query.filters)
        self.assertIn(("visibility", "public"), client.query.filters)

    def test_supabase_gallery_inventory_requests_an_exact_bounded_page(self) -> None:
        client = _GalleryInventoryClient()
        repository = SupabaseRepository(client)

        rows, total = repository.list_project_gallery_inventory_page(
            "user-a", visibility=None, limit=6, offset=12
        )

        self.assertEqual(37, total)
        self.assertEqual(["project-13", "project-14"], [row.project_id for row in rows])
        self.assertEqual("exact", client.query.count_mode)
        self.assertEqual((12, 17), client.query.requested_range)
        self.assertIn(("status", "active"), client.query.filters)
        self.assertIn(("owner_user_id", "user-a"), client.query.filters)

    def test_supabase_repository_searches_titles_and_prompts(self) -> None:
        client = _ProjectPageClient()
        repository = SupabaseRepository(client)

        repository.list_generated_projects_page(
            None,
            visibility="public",
            limit=6,
            offset=0,
            search="motor, controller",
        )

        self.assertEqual(
            'title.ilike."*motor, controller*",prompt.ilike."*motor, controller*"',
            client.query.or_filter,
        )

    def test_legacy_job_import_is_idempotent_and_does_not_overwrite_primary_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary_path = Path(directory) / "forma.db"
            legacy_path = Path(directory) / "forma_jobs.db"
            provider = create_sqlite_provider(
                source="test",
                url=f"sqlite:///{primary_path}",
                import_legacy_jobs=False,
            )
            provider.initialize()
            self._create_legacy_job_database(legacy_path)

            assert provider.engine is not None
            imported_first = import_legacy_job_database(provider.engine, str(legacy_path))
            imported_second = import_legacy_job_database(provider.engine, str(legacy_path))

            with provider.engine.connect() as connection:
                row = connection.exec_driver_sql(
                    "SELECT status, payload_json FROM a2a_jobs WHERE job_id = 'job_legacy'"
                ).one()
            provider.dispose()

        self.assertEqual(1, imported_first)
        self.assertEqual(0, imported_second)
        self.assertEqual("succeeded", row[0])
        self.assertIn("legacy", row[1])

    def test_user_training_preference_is_queryable_by_owner_and_opt_out_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            repository = SqlAlchemyRepository(provider.session_factory)
            provider.initialize()
            original_repository = database._DATABASE_REPOSITORY
            try:
                database._DATABASE_REPOSITORY = repository
                default_settings = database.get_user_settings("user_default")
                opted_out = database.set_user_model_training_preference(
                    "user_opted_out",
                    allow_model_training=False,
                    updated_at="2026-07-27T20:00:00Z",
                )
                database.set_user_model_training_preference(
                    "user_allowed",
                    allow_model_training=True,
                    updated_at="2026-07-27T20:01:00Z",
                )
                opted_out_ids = database.list_model_training_opt_out_user_ids()
            finally:
                database._DATABASE_REPOSITORY = original_repository
                provider.dispose()

        self.assertIsNone(default_settings)
        self.assertTrue(opted_out.model_training_opt_out)
        self.assertEqual(["user_opted_out"], opted_out_ids)

    def test_project_saves_and_remixes_are_counted_from_persisted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = create_sqlite_provider(
                source="test primary",
                url=f"sqlite:///{Path(directory) / 'forma.db'}",
                import_legacy_jobs=False,
            )
            assert provider.session_factory is not None
            repository = SqlAlchemyRepository(provider.session_factory)
            provider.initialize()
            original_repository = database._DATABASE_REPOSITORY
            try:
                database._DATABASE_REPOSITORY = repository
                source_id = str(uuid.uuid4())
                database.save_generated_project(
                    project_id=source_id,
                    title="Desk lamp",
                    prompt="Build a desk lamp.",
                    hardware_ir={"assembly_metadata": {}},
                    created_at="2026-08-18T12:00:00Z",
                    chat_id="chat-source",
                    owner_user_id="owner-a",
                    visibility="public",
                )
                first_save = database.save_project_for_user(source_id, "user-b")
                second_save = database.save_project_for_user(source_id, "user-b")
                database.save_project_for_user(source_id, "user-c")
                remixed = database.remix_generated_project(source_id, "user-b")
                remixed_id = remixed.project_id if remixed is not None else None
                remixed_owner = getattr(remixed, "owner_user_id", None) if remixed is not None else None
                remixed_source = (
                    ((remixed.hardware_ir or {}).get("assembly_metadata") or {}).get("source_project_id")
                    if remixed is not None
                    else None
                )
                engagement = database.project_engagement_for_ids([source_id], "user-b")
                unsaved = database.unsave_project_for_user(source_id, "user-b")
                after_unsave = database.project_engagement_for_ids([source_id], "user-b")
            finally:
                database._DATABASE_REPOSITORY = original_repository
                provider.dispose()

        self.assertTrue(first_save["saved"])
        self.assertEqual(1, first_save["save_count"])
        self.assertTrue(second_save["saved"])
        self.assertEqual(1, second_save["save_count"])
        self.assertIsNotNone(remixed_id)
        self.assertNotEqual(source_id, remixed_id)
        self.assertEqual("user-b", remixed_owner)
        self.assertEqual(source_id, remixed_source)
        self.assertEqual(2, engagement[source_id]["save_count"])
        self.assertEqual(1, engagement[source_id]["remix_count"])
        self.assertTrue(engagement[source_id]["saved"])
        self.assertFalse(unsaved["saved"])
        self.assertEqual(1, unsaved["save_count"])
        self.assertFalse(after_unsave[source_id]["saved"])
        self.assertEqual(1, after_unsave[source_id]["save_count"])
        self.assertEqual(1, after_unsave[source_id]["remix_count"])

    @staticmethod
    def _create_legacy_job_database(path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE a2a_jobs (
                    job_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    correlation_id TEXT,
                    action TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    status TEXT NOT NULL,
                    server_owned INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    payload_json TEXT,
                    result_summary_json TEXT,
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO a2a_jobs (
                    job_id, message_id, action, sender, recipient, status,
                    server_owned, created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job_legacy",
                    "msg_legacy",
                    "forma.generate_project",
                    "test",
                    "forma",
                    "succeeded",
                    1,
                    "2026-07-01T00:00:00Z",
                    "2026-07-01T00:00:01Z",
                    '{"prompt":"legacy"}',
                ),
            )
            connection.commit()


class CliProjectDeliveryPersistenceTests(unittest.TestCase):
    def _provider_and_repo(self) -> tuple[Any, Any]:
        provider = create_sqlite_provider(
            source="test cli project delivery persistence",
            url="sqlite:///:memory:",
            import_legacy_jobs=False,
        )
        provider.initialize()
        return provider, SqlAlchemyRepository(provider.session_factory)

    def test_cli_project_delivery_session_round_trip(self) -> None:
        _, repo = self._provider_and_repo()
        record = {
            "delivery_id": "delivery-a",
            "project_id": "project-a",
            "owner_user_id": "user-a",
            "idempotency_key": "key-a",
            "revision_id": "rev-a",
            "revision": 3,
            "parent_revision_id": "rev-b",
            "manifest_json": {"project_id": "project-a", "visibility": "private"},
            "manifest_digest": "digest-a",
            "status": "pending",
            "receipt_json": None,
            "created_at": "2026-01-01T00:00:00Z",
            "completed_at": None,
        }
        created = repo.insert_cli_project_delivery(record)
        self.assertEqual("delivery-a", created.delivery_id)

        replay = repo.insert_cli_project_delivery(record)
        self.assertEqual("delivery-a", replay.delivery_id)

        delivery = repo.get_cli_project_delivery("project-a", "user-a", "key-a")
        self.assertEqual("delivery-a", delivery.delivery_id)
        self.assertEqual("rev-b", delivery.parent_revision_id)
        self.assertEqual("digest-a", delivery.manifest_digest)

        by_id = repo.get_cli_project_delivery_by_id("delivery-a")
        self.assertEqual("project-a", by_id.project_id)

        updated = repo.update_cli_project_delivery(
            "delivery-a",
            "user-a",
            {"status": "complete", "receipt_json": {"delivery_id": "delivery-a", "status": "complete"}, "completed_at": "2026-01-01T00:00:01Z"},
        )
        self.assertEqual("complete", updated.status)
        self.assertEqual({"delivery_id": "delivery-a", "status": "complete"}, updated.receipt_json)

        self.assertEqual(["delivery-a"], [item.delivery_id for item in repo.list_cli_project_deliveries("user-a")])
        self.assertEqual([], repo.list_cli_project_deliveries("user-b"))
        self.assertIsNone(repo.get_cli_project_delivery_by_id("missing"))

    def test_project_publish_audit_round_trip(self) -> None:
        _, repo = self._provider_and_repo()
        audit = {
            "id": "audit-1",
            "project_id": "project-a",
            "owner_user_id": "user-a",
            "acting_user_id": "user-a",
            "visibility_before": "private",
            "created_at": "2026-01-01T00:00:00Z",
        }
        repo.record_project_publish_audit(audit)
        audits = repo.list_project_publish_audits("project-a", "user-a")
        self.assertEqual(["audit-1"], [item.id for item in audits])
        self.assertEqual("private", audits[0].visibility_before)
        self.assertEqual([], repo.list_project_publish_audits("project-a", "user-b"))

    def test_database_publish_cli_project_flips_private_identity_and_records_audit(self) -> None:
        now_iso = "2026-01-01T00:00:00Z"
        provider = create_sqlite_provider(
            source="test cli project publish helper",
            url="sqlite:///:memory:",
            import_legacy_jobs=False,
        )
        provider.initialize()
        repo = SqlAlchemyRepository(provider.session_factory)
        repo.insert_cli_project_revision(
            {
                "project_id": "project-a",
                "workspace_id": None,
                "owner_user_id": "user-a",
                "creation_channel": "cli",
                "visibility": "private",
                "title": "Delivered project",
                "current_revision": 0,
                "current_revision_id": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            },
            {
                "revision_id": "rev-a",
                "project_id": "project-a",
                "owner_user_id": "user-a",
                "revision": 1,
                "parent_revision_id": None,
                "manifest_json": {"project_id": "project-a", "visibility": "private"},
                "created_at": now_iso,
            },
            expected_revision_id=None,
        )
        with (
            patch.object(database, "_DATABASE_REPOSITORY", repo),
            patch.object(database, "invalidate_project_lists"),
        ):
            result = database.publish_cli_project("project-a", "user-a", acting_user_id="user-a")

        self.assertTrue(result["published"])
        self.assertEqual("private", result["visibility_before"])
        identity = repo.get_project_identity("project-a")
        self.assertEqual("public", identity["visibility"])
        cli_project = repo.get_cli_project("project-a", "user-a")
        self.assertEqual("public", cli_project.visibility)
        audits = repo.list_project_publish_audits("project-a", "user-a")
        self.assertEqual(1, len(audits))
        self.assertEqual("user-a", audits[0].acting_user_id)

        with (
            patch.object(database, "_DATABASE_REPOSITORY", repo),
            patch.object(database, "invalidate_project_lists"),
        ):
            already = database.publish_cli_project("project-a", "user-a", acting_user_id="user-a")

        self.assertFalse(already["published"])
        self.assertEqual("public", repo.get_cli_project("project-a", "user-a").visibility)
        self.assertEqual(1, len(repo.list_project_publish_audits("project-a", "user-a")))

    def test_database_publish_rejects_foreign_owner_and_missing_identity(self) -> None:
        provider = create_sqlite_provider(
            source="test cli project publish guards",
            url="sqlite:///:memory:",
            import_legacy_jobs=False,
        )
        provider.initialize()
        repo = SqlAlchemyRepository(provider.session_factory)
        repo.upsert_project_identity(
            {
                "project_id": "project-a",
                "owner_user_id": "user-a",
                "creation_channel": "cli",
                "title": "Delivered project",
                "prompt": "",
                "chat_id": None,
                "workspace_id": None,
                "visibility": "private",
                "status": "active",
                "current_revision": 1,
                "current_revision_id": "rev-a",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
        with patch.object(database, "_DATABASE_REPOSITORY", repo):
            with self.assertRaisesRegex(ValueError, "owned by another user"):
                database.publish_cli_project("project-a", "user-b", acting_user_id="user-b")
            with self.assertRaisesRegex(ValueError, "not found"):
                database.publish_cli_project("missing-project", "user-a", acting_user_id="user-a")


class _SchemaResponse:
    data: list[dict[str, object]] = []


class _SchemaQuery:
    def __init__(self, client: "_SchemaClient", table: str) -> None:
        self._client = client
        self._table = table

    def select(self, projection: str) -> "_SchemaQuery":
        self._client.projections[self._table] = projection
        return self

    def limit(self, _limit: int) -> "_SchemaQuery":
        return self

    def execute(self) -> _SchemaResponse:
        if self._client.failing_table == self._table:
            raise self._client.failure
        return _SchemaResponse()


class _SchemaClient:
    def __init__(
        self,
        failing_table: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.failing_table = failing_table
        self.failure = failure or RuntimeError("missing table or column")
        self.projections: dict[str, str] = {}

    def table(self, table: str) -> _SchemaQuery:
        return _SchemaQuery(self, table)


class _ProjectPageResponse:
    data = [
        {"project_id": "project-13"},
        {"project_id": "project-14"},
    ]
    count = 37


class _ProjectPageQuery:
    def __init__(self) -> None:
        self.count_mode: str | None = None
        self.requested_range: tuple[int, int] | None = None
        self.filters: list[tuple[str, str]] = []
        self.or_filter: str | None = None

    def select(self, _projection: str, *, count: str | None = None) -> "_ProjectPageQuery":
        self.count_mode = count
        return self

    def eq(self, field: str, value: str) -> "_ProjectPageQuery":
        self.filters.append((field, value))
        return self

    def or_(self, expression: str) -> "_ProjectPageQuery":
        self.or_filter = expression
        return self

    def order(self, _field: str, *, desc: bool = False) -> "_ProjectPageQuery":
        return self

    def range(self, start: int, end: int) -> "_ProjectPageQuery":
        self.requested_range = (start, end)
        return self

    def execute(self) -> _ProjectPageResponse:
        return _ProjectPageResponse()


class _ProjectPageClient:
    def __init__(self) -> None:
        self.query = _ProjectPageQuery()

    def table(self, table: str) -> _ProjectPageQuery:
        assert table == "generated_projects"
        return self.query


class _ProjectIdentityResponse:
    data = [{"project_id": "project-a", "visibility": "private"}]


class _ProjectIdentityQuery:
    def select(self, _projection: str) -> "_ProjectIdentityQuery":
        return self

    def eq(self, _field: str, _value: str) -> "_ProjectIdentityQuery":
        return self

    def limit(self, _limit: int) -> "_ProjectIdentityQuery":
        return self

    def execute(self) -> _ProjectIdentityResponse:
        return _ProjectIdentityResponse()


class _ProjectIdentityClient:
    def table(self, table: str) -> _ProjectIdentityQuery:
        assert table == "projects"
        return _ProjectIdentityQuery()


class _GalleryInventoryClient:
    def __init__(self) -> None:
        self.query = _ProjectPageQuery()

    def table(self, table: str) -> _ProjectPageQuery:
        assert table == "project_gallery_inventory"
        return self.query


if __name__ == "__main__":
    unittest.main()
