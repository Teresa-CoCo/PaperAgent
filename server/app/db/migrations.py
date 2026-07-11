"""Lightweight migration system for SQLite schema evolution.

Migrations are Python functions registered via @migration(version).
They run in order, tracked by a schema_migrations table.
"""
import sqlite3
from typing import Callable

from app.core.logging import log_event

_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = []


def migration(version: int, description: str = "") -> Callable:
    """Register a migration. version must be unique and sequential."""
    def decorator(func: Callable[[sqlite3.Connection], None]) -> Callable[[sqlite3.Connection], None]:
        _MIGRATIONS.append((version, description, func))
        return func
    return decorator


def run_migrations(connection: sqlite3.Connection) -> int:
    """Run all pending migrations. Returns the number of migrations applied."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = connection.execute("SELECT version FROM schema_migrations").fetchall()
    applied_versions = {
        row["version"] if isinstance(row, dict) else row[0]
        for row in applied
    }

    count = 0
    for version, description, func in sorted(_MIGRATIONS, key=lambda m: m[0]):
        if version in applied_versions:
            continue
        try:
            func(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, description) VALUES(?, ?)",
                (version, description),
            )
            log_event("info", "migration_applied", version=version, description=description)
            count += 1
        except Exception as exc:
            log_event("error", "migration_failed", version=version, description=description, error=str(exc))
            raise
    return count


@migration(1, "initial baseline — schema already applied via schema.sql")
def _migration_1(connection: sqlite3.Connection) -> None:
    pass
