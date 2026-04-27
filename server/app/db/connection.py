import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import get_settings


def dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    fields = [column[0] for column in cursor.description]
    return {key: row[index] for index, key in enumerate(fields)}


def connect() -> sqlite3.Connection:
    settings = get_settings()
    connection = sqlite3.connect(settings.database_path, check_same_thread=False)
    connection.row_factory = dict_factory
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    with transaction() as connection:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        ensure_column(connection, "crawl_job_steps", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(connection, "crawl_job_steps", "next_run_at", "TEXT")
        ensure_column(connection, "chat_missions", "updated_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memories (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              agent_key TEXT NOT NULL,
              memory_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(user_id, agent_key)
            )
            """
        )


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
