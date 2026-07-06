import os
import tempfile
from pathlib import Path

import pytest

_tmpdir: tempfile.TemporaryDirectory | None = None


def pytest_configure() -> None:
    global _tmpdir
    _tmpdir = tempfile.TemporaryDirectory(prefix="paperagent_test_")
    db_path = Path(_tmpdir.name) / "test.sqlite3"
    storage = Path(_tmpdir.name) / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ["STORAGE_ROOT"] = str(storage)
    os.environ["RAG_CHROMA_PATH"] = str(Path(_tmpdir.name) / "chroma")
    os.environ["LLM_API_KEY"] = "test-key"
    os.environ["PADDLEOCR_TOKEN"] = "test-token"

    from app.core.config import get_settings
    get_settings.cache_clear()
    get_settings()

    from app.db.connection import init_db, close_all_connections
    close_all_connections()
    init_db()


def pytest_unconfigure() -> None:
    global _tmpdir
    from app.db.connection import close_all_connections
    close_all_connections()
    if _tmpdir:
        _tmpdir.cleanup()
        _tmpdir = None


@pytest.fixture(autouse=True)
def _reset_db():
    from app.db.connection import transaction
    with transaction() as conn:
        conn.execute("DELETE FROM chat_messages")
        conn.execute("DELETE FROM chat_sessions")
        conn.execute("DELETE FROM chat_daily_usage")
        conn.execute("DELETE FROM agent_memories")
        conn.execute("DELETE FROM langgraph_store_items")
    yield
