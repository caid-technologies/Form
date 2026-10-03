from __future__ import annotations

from typing import Tuple

from forma_core.jobs.schema import JOB_TABLE_CONTRACT
from forma_core.persistence.base import TableContract
from forma_core.opencode.schema import OPENCODE_TABLE_CONTRACTS

# This is the schema surface used through both the local SQLite provider and
# the hosted Supabase provider. A Supabase startup projection checks columns as
# well as table visibility, so schema drift fails at startup rather than during
# an unrelated request.
APPLICATION_SCHEMA: Tuple[TableContract, ...] = (
    TableContract(
        "component_templates",
        ("id", "part_number", "name", "category", "description", "price", "sourcing_url", "pins", "use_cases"),
    ),
    TableContract(
        "projects",
        (
            "project_id", "owner_user_id", "creation_channel", "title", "prompt", "chat_id", "workspace_id",
            "visibility", "status", "created_at", "updated_at",
        ),
    ),
    TableContract(
        "project_gallery_inventory",
        (
            "project_id", "owner_user_id", "creation_channel", "title", "prompt", "chat_id", "workspace_id",
            "visibility", "status", "created_at", "updated_at", "source", "revision_id", "revision",
            "revision_payload_json", "revision_created_at", "legacy_hardware_ir", "legacy_id",
        ),
    ),
    TableContract(
        "generated_projects",
        (
            "id", "project_id", "chat_id", "owner_user_id", "creation_channel", "visibility", "title", "prompt", "hardware_ir", "created_at",
            "status", "deleted_at", "deletion_requested_by", "purge_after", "purge_started_at", "purge_completed_at",
            "deletion_error",
        ),
    ),
    TableContract(
        "design_briefs",
        (
            "id", "design_brief_id", "project_id", "conversation_id", "owner_user_id", "brief_version",
            "schema_version", "previous_version", "payload_json", "created_at",
        ),
    ),
    TableContract(
        "project_workflows",
        ("project_id", "owner_user_id", "state", "revision", "created_at", "updated_at"),
    ),
    TableContract(
        "project_workflow_transitions",
        (
            "id", "project_id", "owner_user_id", "from_state", "to_state", "actor_type", "actor_id", "reason",
            "idempotency_key", "revision", "created_at",
        ),
    ),
    TableContract(
        "project_builds",
        (
            "id", "project_id", "owner_user_id", "design_brief_id", "brief_version", "brief_snapshot_json",
            "mode", "readiness_result_json", "introduced_assumptions_json", "warnings_json", "transition_id",
            "idempotency_key", "initiated_by", "created_at",
        ),
    ),
    TableContract(
        "worker_execution_plans",
        (
            "id", "project_id", "owner_user_id", "correlation_id", "status", "max_concurrency",
            "state_json", "created_at", "updated_at", "completed_at",
        ),
    ),
    TableContract(
        "project_revisions",
        (
            "id", "project_id", "owner_user_id", "revision", "parent_revision", "design_brief_id",
            "design_brief_version", "source_job_id", "payload_json", "created_at",
        ),
    ),
    TableContract(
        "cli_projects",
        (
            "project_id", "workspace_id", "owner_user_id", "creation_channel", "title",
            "current_revision", "current_revision_id", "visibility", "created_at", "updated_at",
        ),
    ),
    TableContract(
        "cli_project_revisions",
        (
            "revision_id", "project_id", "owner_user_id", "revision", "parent_revision_id",
            "manifest_json", "created_at",
        ),
    ),
    TableContract(
        "cli_device_authorizations",
        (
            "device_code_hash", "user_code_hash", "status", "expires_at", "owner_user_id", "provider",
            "email", "display_name", "consumed", "created_at",
        ),
    ),
    TableContract(
        "cli_token_sessions",
        (
            "token_hash", "token_type", "refresh_token_hash", "owner_user_id", "provider", "email",
            "display_name", "expires_at", "revoked_at", "created_at",
        ),
    ),
    TableContract(
        "project_validation_reports",
        (
            "id", "project_id", "owner_user_id", "project_revision", "design_brief_id",
            "design_brief_version", "source_job_id", "revalidation_of_report_id", "payload_json", "created_at",
        ),
    ),
    TableContract(
        "project_contribution_consents",
        (
            "id", "project_id", "user_id", "workspace_id", "consent_version", "permitted_purposes", "granted_at",
            "withdrawn_at", "snapshot_created_at", "sanitized_at", "anonymized_at", "purged_at",
        ),
    ),
    TableContract(
        "project_contribution_snapshots",
        (
            "id", "source_project_id", "consent_record_id", "sanitization_version", "contribution_status",
            "payload_json", "created_at", "sanitized_at", "anonymized_at", "purged_at",
        ),
    ),
    TableContract(
        "project_deletion_audit",
        ("id", "project_id", "acting_user_id", "action", "status", "policy_version", "details_json", "created_at"),
    ),
    TableContract(
        "project_publish_audit",
        ("id", "project_id", "owner_user_id", "acting_user_id", "visibility_before", "created_at"),
    ),
    TableContract(
        "cli_project_deliveries",
        (
            "delivery_id", "project_id", "owner_user_id", "idempotency_key", "revision_id", "revision",
            "parent_revision_id", "manifest_json", "status", "receipt_json", "created_at", "completed_at",
        ),
    ),
    TableContract(
        "project_chats",
        ("id", "chat_id", "owner_user_id", "title", "messages", "created_at", "updated_at"),
    ),
    JOB_TABLE_CONTRACT,
    *OPENCODE_TABLE_CONTRACTS,
    TableContract(
        "alpha_signups",
        ("id", "name", "email", "organization", "additional_info", "source", "metadata_json", "created_at"),
    ),
    TableContract(
        "user_integration_configs",
        ("owner_user_id", "encrypted_config", "encryption_key_id", "version", "created_at", "updated_at"),
    ),
    TableContract(
        "workspace_integration_configs",
        ("config_key", "encrypted_config", "encryption_key_id", "version", "created_at", "updated_at"),
    ),
    TableContract(
        "user_settings",
        ("id", "owner_user_id", "model_training_opt_out", "created_at", "updated_at"),
    ),
    TableContract(
        "project_saves",
        ("id", "project_id", "owner_user_id", "created_at"),
    ),
    TableContract(
        "project_remixes",
        ("id", "remix_project_id", "source_project_id", "owner_user_id", "created_at"),
    ),
)
