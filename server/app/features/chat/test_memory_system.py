from app.db.connection import init_db
from app.features.chat.memory_system import ConversationMemoryManager
from app.features.chat.persistence import SQLiteStore
from app.features.tools.llm import LLMClient


def test_sqlite_store_round_trip_and_search() -> None:
    init_db()
    store = SQLiteStore()
    namespace = ("test_memory", "round_trip")
    store.put(namespace, "item-1", {"summary": "RAG agent memory", "tags": ["rag", "agent"]})

    item = store.get(namespace, "item-1")

    assert item is not None
    assert item.value["summary"] == "RAG agent memory"
    matches = store.search(("test_memory",), query="rag", limit=5)
    assert any(match.key == "item-1" for match in matches)


def test_memory_manager_load_bundle_reads_saved_items() -> None:
    init_db()
    store = SQLiteStore()
    user_id = "memory-bundle-user"
    session_id = "memory-bundle-session"
    store.put(("memory", "session", user_id), session_id, {"summary": "Session summary", "open_questions": ["Q1"]})
    store.put(("memory", "profile", user_id), "profile", {"interests": ["rag"], "goals": ["survey"]})
    store.put(
        ("memory", "episode", user_id),
        "episode-1",
        {"summary": "Discussed RAG retrieval", "topics": ["rag", "retrieval"], "source_refs": ["paper_id=1"]},
    )

    bundle = ConversationMemoryManager(LLMClient()).load_bundle(store, user_id, session_id, "rag retrieval")

    assert bundle.session.summary == "Session summary"
    assert bundle.profile.interests == ["rag"]
    assert bundle.episodes
    assert bundle.episodes[0].summary == "Discussed RAG retrieval"
