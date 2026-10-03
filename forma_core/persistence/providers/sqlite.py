from __future__ import annotations

import sqlite3
import threading
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from forma_core.persistence.base import DatabaseProvider, DatabaseSchemaError
from forma_core.persistence.schema import APPLICATION_SCHEMA


class SQLiteProvider(DatabaseProvider):
    backend = "sqlite"

    def __init__(
        self,
        *,
        source: str,
        url: str,
        engine: Engine,
        import_legacy_jobs: bool = True,
    ) -> None:
        self.source = source
        self.url = url
        self.engine = engine
        self.session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self._import_legacy_jobs = import_legacy_jobs
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        from forma_core.jobs.persistence import DBA2AJob  # noqa: F401
        from forma_core.opencode.persistence import (  # noqa: F401
            DBOpenCodeCommand,
            DBOpenCodeEvent,
            DBOpenCodeSession,
        )
        from forma_core.persistence.migrations import migrate_sqlite_schema
        from forma_core.persistence.models import Base

        with self._initialize_lock:
            if self._initialized:
                return
            Base.metadata.create_all(bind=self.engine)
            migrate_sqlite_schema(
                self.engine,
                import_legacy_jobs=self._import_legacy_jobs,
            )
            inspector = inspect(self.engine)
            for table in APPLICATION_SCHEMA:
                if not inspector.has_table(table.name):
                    raise DatabaseSchemaError(f"SQLite schema is missing required table {table.name!r}.")
                available_columns = {column["name"] for column in inspector.get_columns(table.name)}
                missing_columns = set(table.required_columns) - available_columns
                if missing_columns:
                    raise DatabaseSchemaError(
                        f"SQLite table {table.name!r} is missing required columns: "
                        f"{', '.join(sorted(missing_columns))}."
                    )
            self._initialized = True

    def connect_dbapi(self) -> Any:
        """Return a SQLite DB-API connection for SQLite-only adapters."""

        connection = self.engine.raw_connection()
        connection.driver_connection.row_factory = sqlite3.Row
        return connection

    def dispose(self) -> None:
        """Release pooled SQLite connections owned by this provider."""

        self.engine.dispose()

    def describe(self) -> dict[str, Any]:
        config = super().describe()
        config.update({"client": "sqlite/sqlalchemy", "database": make_url(self.url).database})
        return config


def create_sqlite_provider(
    *,
    source: str,
    url: str,
    import_legacy_jobs: bool = True,
) -> SQLiteProvider:
    from sqlalchemy import create_engine

    engine_options: dict[str, Any] = {"connect_args": {"check_same_thread": False, "timeout": 30.0}}
    if make_url(url).database in {None, "", ":memory:"}:
        engine_options["poolclass"] = StaticPool
    engine = create_engine(url, **engine_options)

    @event.listens_for(engine, "connect")
    def configure_sqlite_connection(dbapi_connection: sqlite3.Connection, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if make_url(url).database not in {None, "", ":memory:"}:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    return SQLiteProvider(
        source=source,
        url=url,
        engine=engine,
        import_legacy_jobs=import_legacy_jobs,
    )
