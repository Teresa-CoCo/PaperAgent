import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from app.core.errors import AppError
from app.db.connection import transaction
from app.features.chat.agents import agent_catalog
from app.features.chat.conversation import ChatConversationBuilder, citation_report
from app.features.chat.memory import AgentMemoryStore
from app.features.chat.persistence import SQLiteCheckpointer, SQLiteStore
from app.features.chat.workflow import PaperAceWorkflowEngine, WorkflowRequest
from app.features.papers.arxiv_tool import ArxivTool
from app.features.papers.service import PaperService
from app.features.tools.brave_search import BraveSearchTool
from app.features.tools.llm import LLMClient
from app.features.daily_papers.service import DailyPaperRAGStore
from app.features.users.service import UserPreferenceService, ensure_user


_mission_queue_lock = asyncio.Lock()
PAPER_ACE_SCOPE = "paper_ace"


class ChatService:
    def __init__(self) -> None:
        self.llm = LLMClient()
        self.papers = PaperService()
        self.preferences = UserPreferenceService()
        self.search_tool = BraveSearchTool()
        self.arxiv_tool = ArxivTool()
        self.agent_memory = AgentMemoryStore()
        self.daily_rag = DailyPaperRAGStore()
        self.builder = ChatConversationBuilder(self.papers)
        self.memory_store = SQLiteStore()
        self.checkpointer = SQLiteCheckpointer()
        self.workflow = PaperAceWorkflowEngine(
            llm=self.llm,
            papers=self.papers,
            preferences=self.preferences,
            search_tool=self.search_tool,
            arxiv_tool=self.arxiv_tool,
            agent_memory=self.agent_memory,
            daily_rag=self.daily_rag,
        )

    def create_session(self, user_id: str, scope: str, paper_id: int | None = None, title: str = "") -> dict:
        ensure_user(user_id)
        scope = self._normalize_mode(scope)
        session_id = str(uuid.uuid4())
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_sessions(id, user_id, scope, paper_id, title)
                VALUES(?, ?, ?, ?, ?)
                """,
                (session_id, user_id, scope, paper_id, title or "Paper Ace Paper"),
            )
        return {"id": session_id, "scope": scope, "paperId": paper_id, "title": title or "Paper Ace Paper"}

    def list_sessions(self, user_id: str) -> list[dict]:
        ensure_user(user_id)
        with transaction() as connection:
            rows = connection.execute(
                """
                SELECT
                  s.*,
                  (
                    SELECT content
                    FROM chat_messages
                    WHERE session_id = s.id
                    ORDER BY id DESC
                    LIMIT 1
                  ) AS preview
                FROM chat_sessions s
                WHERE s.user_id = ?
                ORDER BY s.updated_at DESC
                """,
                (user_id,),
            ).fetchall()
            mission_rows = connection.execute(
                """
                SELECT *
                FROM chat_missions
                WHERE user_id = ?
                  AND id IN (
                    SELECT MAX(id)
                    FROM chat_missions
                    WHERE user_id = ?
                    GROUP BY session_id
                  )
                ORDER BY created_at DESC, id DESC
                """,
                (user_id, user_id),
            ).fetchall()
        missions_by_session = {row["session_id"]: self._mission_to_api(row) for row in mission_rows}
        return [
            {
                "id": row["id"],
                "scope": row["scope"],
                "paperId": row["paper_id"],
                "title": row["title"],
                "preview": row["preview"] or "",
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "latestMission": missions_by_session.get(row["id"]),
            }
            for row in rows
        ]

    def messages(self, session_id: str, user_id: str | None = None) -> list[dict]:
        if user_id is not None:
            self._ensure_session_owned(user_id, session_id)
        with transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "selection": row["selection"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def delete_session(self, user_id: str, session_id: str) -> dict:
        ensure_user(user_id)
        with transaction() as connection:
            row = connection.execute(
                "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise AppError("Chat session not found", 404, "chat_session_not_found")
            connection.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        self.workflow.memory_manager.clear_session_memory(self.memory_store, user_id, session_id)
        self.checkpointer.delete_thread(session_id)
        return {"deletedSessionId": session_id}

    async def reply(
        self,
        user_id: str,
        session_id: str,
        message: str,
        paper_id: int | None = None,
        selection: str | None = None,
        attachment_paper_ids: list[int] | None = None,
        mode: str = "paper_ace",
    ) -> dict:
        ensure_user(user_id)
        self._ensure_session_owned(user_id, session_id)
        self.preferences.update_from_text(user_id, message)
        stored_message = self._message_for_storage(message, attachment_paper_ids)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content, selection) VALUES(?, 'user', ?, ?)",
                (session_id, stored_message, selection),
            )
        session_history = self._recent_session_messages(session_id)
        answer = await self.workflow.run(
            WorkflowRequest(
                user_id=user_id,
                session_id=session_id,
                message=message,
                paper_id=paper_id,
                selection=selection,
                session_history=session_history,
                attachment_paper_ids=attachment_paper_ids or [],
                mode=self._normalize_mode(mode),
            )
        )
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content) VALUES(?, 'assistant', ?)",
                (session_id, answer),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
        return {"answer": answer, "messages": self.messages(session_id, user_id)}

    def submit_mission(
        self,
        user_id: str,
        session_id: str,
        message: str,
        paper_id: int | None = None,
        selection: str | None = None,
        attachment_paper_ids: list[int] | None = None,
        mode: str = "paper_ace",
    ) -> dict:
        ensure_user(user_id)
        attachment_paper_ids = attachment_paper_ids or []
        with transaction() as connection:
            session = connection.execute(
                "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if not session:
                raise AppError("Chat session not found", 404, "chat_session_not_found")
            cursor = connection.execute(
                """
                INSERT INTO chat_missions(
                  session_id, user_id, status, mode, message, paper_id, selection, attachment_paper_ids
                )
                VALUES(?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, self._normalize_mode(mode), message, paper_id, selection, json.dumps(attachment_paper_ids)),
            )
            mission_id = int(cursor.lastrowid)
        return self.get_mission(mission_id, user_id)

    def get_mission(self, mission_id: int, user_id: str | None = None) -> dict:
        params: list[object] = [mission_id]
        sql = "SELECT * FROM chat_missions WHERE id = ?"
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        with transaction() as connection:
            row = connection.execute(sql, params).fetchone()
        if not row:
            raise AppError("Mission not found", 404, "mission_not_found")
        return self._mission_to_api(row)

    async def run_mission_queue(self) -> None:
        async with _mission_queue_lock:
            self._recover_interrupted_missions()
            while True:
                mission = self._next_mission()
                if not mission:
                    await asyncio.sleep(1.0)
                    continue
                await self._run_mission(int(mission["id"]))

    async def stream_reply(
        self,
        user_id: str,
        session_id: str,
        message: str,
        paper_id: int | None = None,
        selection: str | None = None,
        attachment_paper_ids: list[int] | None = None,
        mode: str = "paper_ace",
    ) -> AsyncIterator[str]:
        ensure_user(user_id)
        self._ensure_session_owned(user_id, session_id)
        self.preferences.update_from_text(user_id, message)
        stored_message = self._message_for_storage(message, attachment_paper_ids)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content, selection) VALUES(?, 'user', ?, ?)",
                (session_id, stored_message, selection),
            )
        session_history = self._recent_session_messages(session_id)
        text_chunks: list[str] = []
        try:
            async for event in self.workflow.stream(
                WorkflowRequest(
                    user_id=user_id,
                    session_id=session_id,
                    message=message,
                    paper_id=paper_id,
                    selection=selection,
                    session_history=session_history,
                    attachment_paper_ids=attachment_paper_ids or [],
                    mode=self._normalize_mode(mode),
                )
            ):
                text_chunks = self._accumulate_text(event, text_chunks)
                yield event + "\n"
        except Exception as exc:
            user_message = self._user_facing_error(exc)
            error_event = json.dumps({"type": "error", "message": user_message}, ensure_ascii=False)
            if not text_chunks:
                text_chunks.append(user_message)
            yield error_event + "\n"

        answer = "".join(text_chunks)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content) VALUES(?, 'assistant', ?)",
                (session_id, answer),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )

    def agents(self) -> list[dict]:
        return agent_catalog()

    def _accumulate_text(self, event: str, text_chunks: list[str]) -> list[str]:
        try:
            parsed = json.loads(event)
            if parsed.get("type") == "text" and parsed.get("content"):
                text_chunks.append(parsed["content"])
        except json.JSONDecodeError:
            pass
        return text_chunks

    def _recover_interrupted_missions(self) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE chat_missions
                SET status = 'queued',
                    error_message = COALESCE(error_message, 'Recovered after server restart'),
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            )

    def _next_mission(self) -> dict | None:
        with transaction() as connection:
            return connection.execute(
                """
                SELECT *
                FROM chat_missions
                WHERE status = 'queued'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

    async def _run_mission(self, mission_id: int) -> None:
        with transaction() as connection:
            updated = connection.execute(
                """
                UPDATE chat_missions
                SET status = 'running',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP,
                    error_message = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (mission_id,),
            ).rowcount
            row = connection.execute("SELECT * FROM chat_missions WHERE id = ?", (mission_id,)).fetchone()
        if not updated or not row:
            return

        try:
            attachment_paper_ids = json.loads(row["attachment_paper_ids"] or "[]")
            await self.reply(
                user_id=row["user_id"],
                session_id=row["session_id"],
                message=row["message"],
                paper_id=row["paper_id"],
                selection=row["selection"],
                attachment_paper_ids=attachment_paper_ids,
                mode=row["mode"],
            )
            with transaction() as connection:
                connection.execute(
                    """
                    UPDATE chat_missions
                    SET status = 'done',
                        updated_at = CURRENT_TIMESTAMP,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (mission_id,),
                )
        except Exception as exc:
            with transaction() as connection:
                connection.execute(
                    """
                    UPDATE chat_missions
                    SET status = 'failed',
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (self._user_facing_error(exc), mission_id),
                )

    def _recent_session_messages(self, session_id: str, limit: int = 16) -> list[dict]:
        with transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM (
                  SELECT * FROM chat_messages
                  WHERE session_id = ?
                  ORDER BY id DESC
                  LIMIT ?
                ) recent
                ORDER BY id ASC
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_session_owned(self, user_id: str, session_id: str) -> None:
        with transaction() as connection:
            row = connection.execute(
                "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
        if not row:
            raise AppError("Chat session not found", 404, "chat_session_not_found")

    def _mission_to_api(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "sessionId": row["session_id"],
            "status": row["status"],
            "mode": row["mode"],
            "message": row["message"],
            "paperId": row["paper_id"],
            "errorMessage": row["error_message"],
            "createdAt": row["created_at"],
            "startedAt": row["started_at"],
            "updatedAt": row["updated_at"],
            "finishedAt": row["finished_at"],
        }

    def _message_for_storage(self, message: str, attachment_paper_ids: list[int] | None = None) -> str:
        attachment_block = self.builder.format_attachment_block(attachment_paper_ids)
        if not attachment_block:
            return message
        return f"{message}\n\n{attachment_block}"

    def _normalize_mode(self, mode: str | None) -> str:
        if mode in {"paper", "ace", PAPER_ACE_SCOPE}:
            return PAPER_ACE_SCOPE
        return PAPER_ACE_SCOPE

    def _user_facing_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if exc.__class__.__name__ == "HTTPStatusError":
            return "模型服务请求失败，Paper Ace Paper 本轮没有生成结果。请重试；如果仍失败，请检查 LLM 接口配置。"
        if message:
            return f"生成失败：{message}"
        return "生成失败：外部服务连接异常"

    def _citation_report(self, answer: str, source_refs: list[str]) -> str:
        return citation_report(answer, source_refs)

    @staticmethod
    def approve_tool_call(tool_call_id: str, approved: bool) -> bool:
        from app.features.tools.registry import resolve_approval

        return resolve_approval(tool_call_id, approved)
