import json
import random
import threading
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.store.base import BaseStore, GetOp, Item, Op, PutOp, Result, SearchItem, SearchOp

from app.db.connection import transaction

_NAMESPACE_SEP = "::"
_SQLITE_LOCK = threading.RLock()


def _utc_now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


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


class SQLiteCheckpointer(BaseCheckpointSaver[str]):
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        with _SQLITE_LOCK:
            with transaction() as connection:
                if checkpoint_id:
                    row = connection.execute(
                        """
                        SELECT *
                        FROM langgraph_checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                        """,
                        (thread_id, checkpoint_ns, checkpoint_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT *
                        FROM langgraph_checkpoints
                        WHERE thread_id = ? AND checkpoint_ns = ?
                        ORDER BY checkpoint_id DESC
                        LIMIT 1
                        """,
                        (thread_id, checkpoint_ns),
                    ).fetchone()
                if not row:
                    return None
                write_rows = connection.execute(
                    """
                    SELECT *
                    FROM langgraph_checkpoint_writes
                    WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                    ORDER BY write_idx ASC
                    """,
                    (thread_id, checkpoint_ns, row["checkpoint_id"]),
                ).fetchall()
        checkpoint = self.serde.loads_typed((row["checkpoint_type"], row["checkpoint_blob"]))
        metadata = self.serde.loads_typed((row["metadata_type"], row["metadata_blob"]))
        pending_writes = [
            (
                write_row["task_id"],
                write_row["channel"],
                self.serde.loads_typed((write_row["value_type"], write_row["value_blob"])),
            )
            for write_row in write_rows
        ]
        parent_checkpoint_id = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": row["checkpoint_id"],
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
                if parent_checkpoint_id
                else None
            ),
            pending_writes=pending_writes,
        )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        with _SQLITE_LOCK:
            with transaction() as connection:
                rows = connection.execute(
                    "SELECT * FROM langgraph_checkpoints ORDER BY checkpoint_id DESC"
                ).fetchall()
        count = 0
        for row in rows:
            checkpoint_config = {
                "configurable": {
                    "thread_id": row["thread_id"],
                    "checkpoint_ns": row["checkpoint_ns"],
                    "checkpoint_id": row["checkpoint_id"],
                }
            }
            if config:
                if row["thread_id"] != config["configurable"]["thread_id"]:
                    continue
                if row["checkpoint_ns"] != config["configurable"].get("checkpoint_ns", ""):
                    continue
                if get_checkpoint_id(config) and row["checkpoint_id"] != get_checkpoint_id(config):
                    continue
            if before and get_checkpoint_id(before) and row["checkpoint_id"] >= get_checkpoint_id(before):
                continue
            metadata = self.serde.loads_typed((row["metadata_type"], row["metadata_blob"]))
            if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                continue
            yield self.get_tuple(checkpoint_config)  # type: ignore[misc]
            count += 1
            if limit is not None and count >= limit:
                break

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        _ = new_versions
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with _SQLITE_LOCK:
            with transaction() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO langgraph_checkpoints(
                      thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                      checkpoint_type, checkpoint_blob, metadata_type, metadata_blob, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint["id"],
                        config["configurable"].get("checkpoint_id"),
                        checkpoint_type,
                        checkpoint_blob,
                        metadata_type,
                        metadata_blob,
                    ),
                )
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with _SQLITE_LOCK:
            with transaction() as connection:
                for idx, (channel, value) in enumerate(writes):
                    write_idx = WRITES_IDX_MAP.get(channel, idx)
                    if write_idx >= 0:
                        existing = connection.execute(
                            """
                            SELECT 1
                            FROM langgraph_checkpoint_writes
                            WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?
                              AND task_id = ? AND write_idx = ?
                            """,
                            (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx),
                        ).fetchone()
                        if existing:
                            continue
                    value_type, value_blob = self.serde.dumps_typed(value)
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO langgraph_checkpoint_writes(
                          thread_id, checkpoint_ns, checkpoint_id, task_id, task_path,
                          write_idx, channel, value_type, value_blob, created_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (
                            thread_id,
                            checkpoint_ns,
                            checkpoint_id,
                            task_id,
                            task_path,
                            write_idx,
                            channel,
                            value_type,
                            value_blob,
                        ),
                    )

    def delete_thread(self, thread_id: str) -> None:
        with _SQLITE_LOCK:
            with transaction() as connection:
                connection.execute(
                    "DELETE FROM langgraph_checkpoint_writes WHERE thread_id = ?",
                    (thread_id,),
                )
                connection.execute(
                    "DELETE FROM langgraph_checkpoints WHERE thread_id = ?",
                    (thread_id,),
                )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        _ = channel
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        next_v = current_v + 1
        next_h = random.random()
        return f"{next_v:032}.{next_h:016}"
