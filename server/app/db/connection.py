import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.config import get_settings

_DB_LOCK = threading.RLock()


def dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    fields = [column[0] for column in cursor.description]
    return {key: row[index] for index, key in enumerate(fields)}


def connect() -> sqlite3.Connection:
    settings = get_settings()
    connection = sqlite3.connect(settings.database_path, check_same_thread=False, timeout=30)
    connection.row_factory = dict_factory
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_workflow_runs (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
              mode TEXT NOT NULL,
              status TEXT NOT NULL,
              message TEXT NOT NULL,
              classification_json TEXT NOT NULL DEFAULT '{}',
              prompt_versions_json TEXT NOT NULL DEFAULT '{}',
              metrics_json TEXT NOT NULL DEFAULT '{}',
              error_message TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              finished_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_agent_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workflow_id TEXT NOT NULL REFERENCES chat_workflow_runs(id) ON DELETE CASCADE,
              agent_key TEXT NOT NULL,
              agent_name TEXT NOT NULL,
              phase TEXT NOT NULL,
              status TEXT NOT NULL,
              prompt_version TEXT NOT NULL DEFAULT '',
              attempt_count INTEGER NOT NULL DEFAULT 0,
              duration_ms INTEGER NOT NULL DEFAULT 0,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL DEFAULT 0,
              estimated_cost_usd REAL NOT NULL DEFAULT 0,
              tool_call_count INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              error_message TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              finished_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_tool_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              workflow_id TEXT NOT NULL REFERENCES chat_workflow_runs(id) ON DELETE CASCADE,
              agent_key TEXT NOT NULL,
              tool_call_id TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              status TEXT NOT NULL,
              duration_ms INTEGER NOT NULL DEFAULT 0,
              approval_required INTEGER NOT NULL DEFAULT 0,
              arguments_json TEXT NOT NULL DEFAULT '{}',
              result_json TEXT NOT NULL DEFAULT '{}',
              error_message TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_daily_usage (
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              usage_date TEXT NOT NULL,
              prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0,
              total_tokens INTEGER NOT NULL DEFAULT 0,
              tool_calls INTEGER NOT NULL DEFAULT 0,
              estimated_cost_usd REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(user_id, usage_date)
            )
            """
        )


def ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
