import uuid
from collections.abc import AsyncIterator

from app.db.connection import transaction
from app.features.papers.service import PaperService
from app.features.tools.brave_search import BraveSearchTool
from app.features.tools.llm import ChatMessage, LLMClient
from app.features.users.service import UserPreferenceService, ensure_user


class ChatService:
    def __init__(self) -> None:
        self.llm = LLMClient()
        self.papers = PaperService()
        self.preferences = UserPreferenceService()
        self.search_tool = BraveSearchTool()

    def create_session(self, user_id: str, scope: str, paper_id: int | None = None, title: str = "") -> dict:
        ensure_user(user_id)
        session_id = str(uuid.uuid4())
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_sessions(id, user_id, scope, paper_id, title)
                VALUES(?, ?, ?, ?, ?)
                """,
                (session_id, user_id, scope, paper_id, title),
            )
        return {"id": session_id, "scope": scope, "paperId": paper_id, "title": title}

    def list_sessions(self, user_id: str) -> list[dict]:
        ensure_user(user_id)
        with transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "scope": row["scope"],
                "paperId": row["paper_id"],
                "title": row["title"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def messages(self, session_id: str) -> list[dict]:
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

    async def reply(
        self,
        user_id: str,
        session_id: str,
        message: str,
        paper_id: int | None = None,
        selection: str | None = None,
        mode: str = "paper",
    ) -> dict:
        ensure_user(user_id)
        self.preferences.update_from_text(user_id, message)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content, selection) VALUES(?, 'user', ?, ?)",
                (session_id, message, selection),
            )

        if mode == "ace":
            answer = await self._ace_reply(user_id, message)
        else:
            answer = await self._paper_reply(message, paper_id, selection)

        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content) VALUES(?, 'assistant', ?)",
                (session_id, answer),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
        return {"answer": answer, "messages": self.messages(session_id)}

    async def stream_reply(
        self,
        user_id: str,
        session_id: str,
        message: str,
        paper_id: int | None = None,
        selection: str | None = None,
        mode: str = "paper",
    ) -> AsyncIterator[str]:
        ensure_user(user_id)
        self.preferences.update_from_text(user_id, message)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content, selection) VALUES(?, 'user', ?, ?)",
                (session_id, message, selection),
            )

        chunks: list[str] = []
        fallback_answer = ""
        try:
            messages = await self._messages_for_reply(user_id, message, paper_id, selection, mode)
            if mode == "ace":
                fallback_answer = self._local_answer(message)
            async for chunk in self.llm.stream(messages):
                chunks.append(chunk)
                yield chunk
        except Exception as exc:
            if fallback_answer:
                error_text = fallback_answer + f"\n\n[LLM 连接失败，已使用本地数据库结果兜底：{exc.__class__.__name__}]"
            else:
                error_text = f"\n\n[生成中断] {exc.__class__.__name__}: {str(exc) or '外部服务连接失败'}"
            chunks.append(error_text)
            yield error_text

        answer = "".join(chunks)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content) VALUES(?, 'assistant', ?)",
                (session_id, answer),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )

    async def _messages_for_reply(
        self,
        user_id: str,
        message: str,
        paper_id: int | None,
        selection: str | None,
        mode: str,
    ) -> list[ChatMessage]:
        if mode == "ace":
            local_results = self.papers.search_local(message, limit=12)
            if not local_results:
                local_results = self.preferences.recommendations(user_id, limit=12)
            recent_results = self.papers.recent_papers(limit=16)
            web_results = await self.search_tool.search(message, count=3) if self._should_use_web(message) else []
            compact_recs = [self._compact_paper(item) for item in local_results]
            compact_recent = [self._compact_paper(item) for item in recent_results]
            return [
                ChatMessage(
                    "system",
                    (
                        "You are Ace Chat, a research agent. First answer from local database papers. "
                        "Only use web_search results when they are supplied. If the user asks for database/local papers, "
                        "do not imply web search was used. The local database papers include keyword matches and the most recently crawled papers. "
                        "If the user asks about recently crawled papers, use recent_database_papers. Recommend papers with arxivId/title and concise reasons in Chinese."
                    ),
                ),
                ChatMessage(
                    "user",
                    f"User question: {message}\nTool decision: {self._tool_decision(message)}\nLocal database papers: {compact_recs}\nRecent database papers: {compact_recent}\nWeb search results: {web_results}",
                ),
            ]

        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "")
        selection_block = f"\nSelected markdown:\n{selection[:3000]}" if selection else ""
        prompt = (
            f"Question: {message}\n"
            f"{selection_block}\n"
            "Retrieved context:\n"
            + "\n---\n".join(context_chunks)
        )
        return [
            ChatMessage("system", "Answer in Chinese. Cite uncertainty. Use only supplied paper context when possible."),
            ChatMessage("user", prompt[:14000]),
        ]

    async def _paper_reply(self, message: str, paper_id: int | None, selection: str | None) -> str:
        return await self.llm.complete("paper-chat", await self._messages_for_reply("", message, paper_id, selection, "paper"), use_cache=False)

    async def _ace_reply(self, user_id: str, message: str) -> str:
        local_results = self.papers.search_local(message, limit=12)
        if not local_results:
            local_results = self.preferences.recommendations(user_id, limit=12)
        recent_results = self.papers.recent_papers(limit=16)
        web_results = await self.search_tool.search(message, count=3) if self._should_use_web(message) else []
        compact_recs = [self._compact_paper(item) for item in local_results]
        compact_recent = [self._compact_paper(item) for item in recent_results]
        return await self.llm.complete(
            "ace-chat",
            [
                ChatMessage(
                    "system",
                    (
                        "You are Ace Chat, a research agent. First answer from local database papers. "
                        "Only use web_search results when they are supplied. The local database papers include keyword matches and the most recently crawled papers. "
                        "If the user asks about recently crawled papers, use recent_database_papers. Recommend papers with arxivId/title and concise reasons in Chinese."
                    ),
                ),
                ChatMessage(
                    "user",
                    f"User question: {message}\nTool decision: {self._tool_decision(message)}\nLocal database papers: {compact_recs}\nRecent database papers: {compact_recent}\nWeb search results: {web_results}",
                ),
            ],
            use_cache=False,
        )

    def _should_use_web(self, message: str) -> bool:
        lowered = message.lower()
        if any(marker in lowered for marker in ["数据库", "本地", "已收藏", "已解析", "库里", "db", "local"]):
            return False
        web_markers = [
            "联网",
            "网页",
            "web",
            "brave",
            "google",
            "搜索一下",
            "网上",
            "新闻",
            "刚刚",
            "实时",
            "外部",
        ]
        return any(marker in lowered for marker in web_markers)

    def _tool_decision(self, message: str) -> dict:
        return {
            "local_database_search": True,
            "web_search": self._should_use_web(message),
            "reason": "默认优先查询本地 SQLite 论文库；仅当用户明确要求联网或外部实时信息时使用 Brave。",
        }

    def _compact_paper(self, paper: dict) -> dict:
        return {
            "id": paper["id"],
            "arxivId": paper["arxivId"],
            "title": paper["title"],
            "category": paper.get("category", ""),
            "publishedAt": paper.get("publishedAt"),
            "tags": paper.get("tags", []),
            "summary": (paper.get("aiSummary") or paper.get("abstract") or "")[:900],
        }

    def _local_answer(self, message: str) -> str:
        results = self.papers.search_local(message, limit=8)
        if not results:
            results = self.papers.recent_papers(limit=8)
        if not results:
            return "本地数据库还没有论文。可以先抓取论文后再问。"
        lines = ["本地数据库中匹配到这些论文："]
        for index, paper in enumerate(results, start=1):
            summary = (paper.get("aiSummary") or paper.get("abstract") or "").strip().replace("\n", " ")
            lines.append(
                f"{index}. {paper['title']} ({paper['arxivId']}, {paper.get('publishedAt', '')[:10]})\n"
                f"   推荐理由：标题、摘要或标签与问题中的关键词匹配。{summary[:180]}"
            )
        return "\n".join(lines)
