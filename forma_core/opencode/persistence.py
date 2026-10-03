"""SQLAlchemy rows for the durable OpenCode connector protocol."""

from sqlalchemy import Column, Integer, String, Text, UniqueConstraint

from forma_core.persistence.models import Base


class DBOpenCodeSession(Base):
    __tablename__ = "opencode_sessions"

    session_id = Column(String, primary_key=True)
    connector_id = Column(String, index=True, nullable=False)
    owner_user_id = Column(String, index=True, nullable=False)
    project_id = Column(String, index=True, nullable=False)
    status = Column(String, index=True, nullable=False)
    capability_nonce = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, index=True, nullable=False)
    last_heartbeat_at = Column(String, nullable=True)
    next_event_sequence = Column(Integer, nullable=False, default=1)


class DBOpenCodeCommand(Base):
    __tablename__ = "opencode_commands"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_opencode_commands_idempotency"),
    )

    command_id = Column(String, primary_key=True)
    session_id = Column(String, index=True, nullable=False)
    connector_id = Column(String, index=True, nullable=False)
    owner_user_id = Column(String, index=True, nullable=False)
    project_id = Column(String, index=True, nullable=False)
    operation = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False)
    status = Column(String, index=True, nullable=False)
    message_digest = Column(String, nullable=False)
    message_ciphertext = Column(Text, nullable=True)
    message_key_id = Column(String, nullable=True)
    model = Column(String(200), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    lease_expires_at = Column(String, nullable=True)
    lease_token_hash = Column(String, nullable=True)
    created_at = Column(String, index=True, nullable=False)
    updated_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=True)


class DBOpenCodeEvent(Base):
    __tablename__ = "opencode_events"
    __table_args__ = (
        UniqueConstraint("session_id", "event_id", name="uq_opencode_events_event_id"),
        UniqueConstraint("session_id", "sequence", name="uq_opencode_events_sequence"),
    )

    event_id = Column(String, primary_key=True)
    session_id = Column(String, index=True, nullable=False)
    owner_user_id = Column(String, index=True, nullable=False)
    project_id = Column(String, index=True, nullable=False)
    sequence = Column(Integer, nullable=False)
    event_json = Column(Text, nullable=False)
    created_at = Column(String, index=True, nullable=False)
