"""Schema contracts for the hosted OpenCode bridge."""

from forma_core.persistence.base import TableContract


OPENCODE_TABLE_CONTRACTS = (
    TableContract(
        "opencode_sessions",
        (
            "session_id", "connector_id", "owner_user_id", "project_id", "status", "capability_nonce",
            "created_at", "updated_at", "last_heartbeat_at", "next_event_sequence",
        ),
    ),
    TableContract(
        "opencode_commands",
        (
            "command_id", "session_id", "connector_id", "owner_user_id", "project_id", "operation",
            "idempotency_key", "status", "message_digest", "message_ciphertext", "message_key_id", "attempt_count", "lease_expires_at",
            "lease_token_hash", "created_at", "updated_at", "completed_at",
        ),
    ),
    TableContract(
        "opencode_events",
        (
            "event_id", "session_id", "owner_user_id", "project_id", "sequence", "event_json", "created_at",
        ),
    ),
)
