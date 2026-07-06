import json
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore, GetOp, Item, Op, PutOp, Result, SearchItem, SearchOp

from app.db.connection import transaction

_NAMESPACE_SEP = "::"
_SQLITE_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _namespace_key(namespace: tuple[str, ...]) -> str:
    return _NAMESPACE_SEP.join(namespace)


def _namespace_tuple(namespace_key: str) -> tuple[str, ...]:
    if not namespace_key:
        return tuple()
    return tuple(part for part in namespace_key.split(_NAMESPACE_SEP) if part)


def _index_text(value: Any) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            for key, item in node.items():
                parts.append(str(key))
                walk(item)
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)
            return
        parts.append(str(node))

    walk(value)
    return " ".join(parts).lower()


def _matches_filter(value: dict[str, Any], filter_dict: dict[str, Any] | None) -> bool:
    if not filter_dict:
        return True
    for key, expected in filter_dict.items():
        actual = value.get(key)
        if isinstance(expected, dict):
            if "$eq" in expected and actual != expected["$eq"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            continue
        if actual != expected:
            return False
    return True


class SQLiteStore(BaseStore):
    def batch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        with _SQLITE_LOCK:
            with transaction() as connection:
                for op in ops:
                    if isinstance(op, GetOp):
                        results.append(self._get(connection, op.namespace, op.key))
                        continue
                    if isinstance(op, SearchOp):
                        results.append(
                            self._search(
                                connection,
                                op.namespace_prefix,
                                query=op.query,
                                filter_dict=op.filter,
                                limit=op.limit,
                                offset=op.offset,
                            )
                        )
                        continue
                    if isinstance(op, PutOp):
                        self._put(connection, op.namespace, op.key, op.value)
                        results.append(None)
                        continue
                    raise NotImplementedError(f"Unsupported store operation: {type(op).__name__}")
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return self.batch(ops)

    def delete_namespace_prefix(self, namespace_prefix: tuple[str, ...]) -> int:
        prefix = _namespace_key(namespace_prefix)
        with _SQLITE_LOCK:
            with transaction() as connection:
                if not prefix:
                    row = connection.execute("SELECT COUNT(*) AS count FROM langgraph_store_items").fetchone()
                    connection.execute("DELETE FROM langgraph_store_items")
                    return int(row["count"] or 0)
                rows = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM langgraph_store_items
                    WHERE namespace = ? OR namespace LIKE ?
                    """,
                    (prefix, f"{prefix}{_NAMESPACE_SEP}%"),
                ).fetchone()
                connection.execute(
                    """
                    DELETE FROM langgraph_store_items
                    WHERE namespace = ? OR namespace LIKE ?
                    """,
                    (prefix, f"{prefix}{_NAMESPACE_SEP}%"),
                )
                return int(rows["count"] or 0)

    def _get(self, connection: Any, namespace: tuple[str, ...], key: str) -> Item | None:
        row = connection.execute(
            """
            SELECT *
            FROM langgraph_store_items
            WHERE namespace = ? AND item_key = ?
            """,
            (_namespace_key(namespace), str(key)),
        ).fetchone()
        if not row:
            return None
        return Item(
            namespace=_namespace_tuple(row["namespace"]),
            key=row["item_key"],
            value=json.loads(row["value_json"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _search(
        self,
        connection: Any,
        namespace_prefix: tuple[str, ...],
        *,
        query: str | None,
        filter_dict: dict[str, Any] | None,
        limit: int,
        offset: int,
    ) -> list[SearchItem]:
        prefix = _namespace_key(namespace_prefix)
        if prefix:
            rows = connection.execute(
                """
                SELECT *
                FROM langgraph_store_items
                WHERE namespace = ? OR namespace LIKE ?
                ORDER BY updated_at DESC
                """,
                (prefix, f"{prefix}{_NAMESPACE_SEP}%"),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM langgraph_store_items ORDER BY updated_at DESC"
            ).fetchall()
        query_tokens = [token for token in (query or "").lower().split() if token]
        matches: list[SearchItem] = []
        for row in rows:
            value = json.loads(row["value_json"] or "{}")
            if not _matches_filter(value, filter_dict):
                continue
            indexed_text = row["indexed_text"] or ""
            score: float | None = None
            if query_tokens:
                token_hits = sum(indexed_text.count(token) for token in query_tokens)
                if token_hits <= 0:
                    continue
                score = float(token_hits)
            matches.append(
                SearchItem(
                    namespace=_namespace_tuple(row["namespace"]),
                    key=row["item_key"],
                    value=value,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                    score=score,
                )
            )
        if query_tokens:
            matches.sort(key=lambda item: (item.score or 0.0, item.updated_at), reverse=True)
        return matches[offset : offset + limit]

    def _put(self, connection: Any, namespace: tuple[str, ...], key: str, value: dict[str, Any] | None) -> None:
        namespace_key = _namespace_key(namespace)
        item_key = str(key)
        if value is None:
            connection.execute(
                "DELETE FROM langgraph_store_items WHERE namespace = ? AND item_key = ?",
                (namespace_key, item_key),
            )
            return
        row = connection.execute(
            """
            SELECT created_at
            FROM langgraph_store_items
            WHERE namespace = ? AND item_key = ?
            """,
            (namespace_key, item_key),
        ).fetchone()
        created_at = row["created_at"] if row else _utc_now().isoformat()
        updated_at = _utc_now().isoformat()
        connection.execute(
            """
            INSERT INTO langgraph_store_items(
              namespace, item_key, value_json, indexed_text, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, item_key) DO UPDATE SET
              value_json = excluded.value_json,
              indexed_text = excluded.indexed_text,
              updated_at = excluded.updated_at
            """,
            (
                namespace_key,
                item_key,
                json.dumps(value, ensure_ascii=False),
                _index_text(value),
                created_at,
                updated_at,
            ),
        )
