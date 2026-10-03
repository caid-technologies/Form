from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from forma_core.jobs.migrations import migrate_job_schema


def migrate_sqlite_schema(engine: Engine, *, import_legacy_jobs: bool = True) -> None:
    """Upgrade the local application schema and delegate job-owned migrations."""

    with engine.begin() as connection:
        project_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(generated_projects)").fetchall()
        }
        if "chat_id" not in project_columns:
            connection.execute(text("ALTER TABLE generated_projects ADD COLUMN chat_id VARCHAR"))
        if "owner_user_id" not in project_columns:
            connection.execute(text("ALTER TABLE generated_projects ADD COLUMN owner_user_id VARCHAR"))
        if "creation_channel" not in project_columns:
            connection.execute(text("ALTER TABLE generated_projects ADD COLUMN creation_channel VARCHAR NOT NULL DEFAULT 'hosted'"))
        if "visibility" not in project_columns:
            connection.execute(
                text("ALTER TABLE generated_projects ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'public'")
            )
        if "status" not in project_columns:
            connection.execute(
                text("ALTER TABLE generated_projects ADD COLUMN status VARCHAR NOT NULL DEFAULT 'active'")
            )
        for column, column_type in (
            ("deleted_at", "VARCHAR"),
            ("deletion_requested_by", "VARCHAR"),
            ("purge_after", "VARCHAR"),
            ("purge_started_at", "VARCHAR"),
            ("purge_completed_at", "VARCHAR"),
            ("deletion_error", "TEXT"),
        ):
            if column not in project_columns:
                connection.execute(text(f"ALTER TABLE generated_projects ADD COLUMN {column} {column_type}"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generated_projects_chat_id ON generated_projects (chat_id)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generated_projects_owner_user_id ON generated_projects (owner_user_id)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generated_projects_creation_channel ON generated_projects (creation_channel)")
        )
        cli_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(cli_projects)").fetchall()
        }
        if cli_columns and "creation_channel" not in cli_columns:
            connection.execute(text("ALTER TABLE cli_projects ADD COLUMN creation_channel VARCHAR NOT NULL DEFAULT 'cli'"))
        if cli_columns and "visibility" not in cli_columns:
            connection.execute(text("ALTER TABLE cli_projects ADD COLUMN visibility VARCHAR NOT NULL DEFAULT 'public'"))
        identity_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(projects)").fetchall()
        }
        for column, column_type in (
            ("current_revision", "INTEGER NOT NULL DEFAULT 0"),
            ("current_revision_id", "VARCHAR"),
            ("deleted_at", "VARCHAR"),
            ("deletion_requested_by", "VARCHAR"),
            ("purge_after", "VARCHAR"),
            ("purge_started_at", "VARCHAR"),
            ("purge_completed_at", "VARCHAR"),
            ("deletion_error", "TEXT"),
        ):
            if column not in identity_columns:
                connection.execute(text(f"ALTER TABLE projects ADD COLUMN {column} {column_type}"))
        connection.execute(text("""
            INSERT OR IGNORE INTO projects (
                project_id, owner_user_id, creation_channel, title, prompt, chat_id, workspace_id,
                visibility, status, created_at, updated_at
            )
            SELECT project_id, owner_user_id, 'hosted', title, prompt, chat_id, NULL,
                   visibility, status, created_at, created_at
            FROM generated_projects
        """))
        if cli_columns:
            connection.execute(text("""
                INSERT OR IGNORE INTO projects (
                    project_id, owner_user_id, creation_channel, title, prompt, chat_id, workspace_id,
                    visibility, status, created_at, updated_at
                )
                SELECT p.project_id, p.owner_user_id, 'cli', p.title,
                       COALESCE(json_extract(r.manifest_json, '$.prompt'), ''), NULL, p.workspace_id,
                       'private', 'active', p.created_at, p.updated_at
                FROM cli_projects p
                LEFT JOIN cli_project_revisions r
                  ON r.revision_id = p.current_revision_id
            """))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generated_projects_visibility ON generated_projects (visibility)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generated_projects_status ON generated_projects (status)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_generated_projects_purge_after ON generated_projects (purge_after)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_projects_purge_after ON projects (purge_after)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_projects_gallery_status_visibility_updated "
                 "ON projects (status, visibility, updated_at DESC)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_project_revisions_project_revision "
                 "ON project_revisions (project_id, revision DESC)")
        )
        opencode_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(opencode_commands)").fetchall()
        }
        if opencode_columns:
            if "model" not in opencode_columns:
                connection.execute(text("ALTER TABLE opencode_commands ADD COLUMN model VARCHAR(200)"))
            if "message_ciphertext" not in opencode_columns:
                connection.execute(text("ALTER TABLE opencode_commands ADD COLUMN message_ciphertext TEXT"))
            if "message_key_id" not in opencode_columns:
                connection.execute(text("ALTER TABLE opencode_commands ADD COLUMN message_key_id VARCHAR"))
        connection.exec_driver_sql("DROP VIEW IF EXISTS project_gallery_inventory")
        connection.exec_driver_sql(
            """
            CREATE VIEW project_gallery_inventory AS
            WITH hosted_latest AS (
                SELECT r.*
                FROM project_revisions r
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM project_revisions newer
                    WHERE newer.project_id = r.project_id
                      AND newer.revision > r.revision
                )
            ), cli_latest AS (
                SELECT r.*
                FROM cli_project_revisions r
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM cli_project_revisions newer
                    WHERE newer.project_id = r.project_id
                      AND newer.revision > r.revision
                )
            )
            SELECT
                p.project_id, p.owner_user_id, p.creation_channel, p.title, p.prompt, p.chat_id,
                p.workspace_id, p.visibility, p.status, p.created_at,
                coalesce(r.created_at, p.updated_at) AS updated_at,
                'canonical' AS source, r.id AS revision_id, r.revision,
                r.payload_json AS revision_payload_json, r.created_at AS revision_created_at,
                NULL AS legacy_hardware_ir, NULL AS legacy_id
            FROM projects p
            JOIN hosted_latest r ON r.project_id = p.project_id
                                     AND r.owner_user_id = p.owner_user_id
            WHERE p.creation_channel <> 'cli'
            UNION ALL
            SELECT
                p.project_id, p.owner_user_id, p.creation_channel, p.title, p.prompt, p.chat_id,
                p.workspace_id, p.visibility, p.status, p.created_at,
                coalesce(r.created_at, p.updated_at) AS updated_at,
                'canonical' AS source, r.revision_id, r.revision,
                r.manifest_json AS revision_payload_json, r.created_at AS revision_created_at,
                NULL AS legacy_hardware_ir, NULL AS legacy_id
            FROM projects p
            JOIN cli_projects cp ON cp.project_id = p.project_id
                                    AND cp.owner_user_id = p.owner_user_id
            JOIN cli_latest r ON r.project_id = cp.project_id
                               AND r.owner_user_id = cp.owner_user_id
            WHERE p.creation_channel = 'cli'
              AND (cp.current_revision_id IS NULL OR r.revision_id = cp.current_revision_id)
            UNION ALL
            SELECT
                g.project_id, g.owner_user_id, g.creation_channel, g.title, g.prompt, g.chat_id,
                NULL AS workspace_id, g.visibility, g.status, g.created_at, g.created_at,
                'legacy' AS source, NULL AS revision_id, NULL AS revision,
                 NULL AS revision_payload_json, NULL AS revision_created_at,
                 g.hardware_ir AS legacy_hardware_ir, g.id AS legacy_id
            FROM generated_projects g
            WHERE g.status = 'active'
              AND NOT EXISTS (
                  SELECT 1
                  FROM project_revisions r
                  WHERE r.project_id = g.project_id
                    AND r.owner_user_id = g.owner_user_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM cli_project_revisions r
                  WHERE r.project_id = g.project_id
                    AND r.owner_user_id = g.owner_user_id
              )
            """
        )

    migrate_job_schema(engine, import_legacy_jobs=import_legacy_jobs)
