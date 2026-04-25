import json
import uuid
from collections.abc import AsyncIterator

from app.db.connection import transaction
from app.features.papers.arxiv_tool import ArxivTool
from app.features.papers.service import PaperService
from app.features.tools.brave_search import BraveSearchTool
from app.features.tools.llm import ChatMessage, LLMClient, LLMResponse
from app.features.tools.registry import (
    ToolContext,
    await_approval,
    cleanup_approval,
    execute_tool,
    is_dangerous_command,
    register_approval,
    tool_definitions,
    approval_decision,
)
from app.features.users.service import UserPreferenceService, ensure_user

MAX_TOOL_TURNS = 6


class ChatService:
    def __init__(self) -> None:
        self.llm = LLMClient()
        self.papers = PaperService()
        self.preferences = UserPreferenceService()
        self.search_tool = BraveSearchTool()
        self.arxiv_tool = ArxivTool()

    def _tool_ctx(self, user_id: str) -> ToolContext:
        return ToolContext(
            user_id=user_id,
            paper_service=self.papers,
            user_preferences=self.preferences,
            brave_search=self.search_tool,
            arxiv_tool=self.arxiv_tool,
        )

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Non-streaming reply (legacy)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Streaming reply (main entrypoint)
    # ------------------------------------------------------------------

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

        text_chunks: list[str] = []
        try:
            if mode == "ace":
                async for event in self._stream_ace_ndjson(user_id, message):
                    text_chunks = self._accumulate_text(event, text_chunks)
                    yield event + "\n"
            else:
                fallback_answer = self._local_answer(user_id, message) if mode == "ace" else ""
                async for event in self._stream_paper_ndjson(message, paper_id, selection):
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

    def _accumulate_text(self, event: str, text_chunks: list[str]) -> list[str]:
        """Extract text content from an NDJSON event for DB storage."""
        try:
            parsed = json.loads(event)
            if parsed.get("type") == "text" and parsed.get("content"):
                text_chunks.append(parsed["content"])
        except json.JSONDecodeError:
            pass
        return text_chunks

    # ------------------------------------------------------------------
    # Paper mode (uses RAG context — no tools)
    # ------------------------------------------------------------------

    async def _stream_paper_ndjson(
        self,
        message: str,
        paper_id: int | None,
        selection: str | None,
    ) -> AsyncIterator[str]:
        """Paper mode — wraps LLM stream in NDJSON text events."""
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "")
        selection_block = f"\nSelected markdown:\n{selection[:3000]}" if selection else ""
        prompt = (
            f"Question: {message}\n"
            f"{selection_block}\n"
            "Retrieved context:\n"
            + "\n---\n".join(context_chunks)
        )
        messages = [
            ChatMessage("system", "Answer in Chinese. Cite uncertainty. Use only supplied paper context when possible."),
            ChatMessage("user", prompt[:14000]),
        ]
        async for chunk in self.llm.stream(messages):
            yield json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)
        yield json.dumps({"type": "done"})

    async def _paper_reply(self, message: str, paper_id: int | None, selection: str | None) -> str:
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "")
        selection_block = f"\nSelected markdown:\n{selection[:3000]}" if selection else ""
        prompt = (
            f"Question: {message}\n"
            f"{selection_block}\n"
            "Retrieved context:\n"
            + "\n---\n".join(context_chunks)
        )
        return await self.llm.complete(
            "paper-chat",
            [
                ChatMessage("system", "Answer in Chinese. Cite uncertainty. Use only supplied paper context when possible."),
                ChatMessage("user", prompt[:14000]),
            ],
            use_cache=False,
        )

    # ------------------------------------------------------------------
    # Ace mode — tool-calling agent
    # ------------------------------------------------------------------

    async def _stream_ace_ndjson(self, user_id: str, message: str) -> AsyncIterator[str]:
        """Ace mode tool-calling loop with NDJSON streaming events.

        Yields JSON lines: tool_start, approval, tool_result, text, done.
        """
        messages = self._ace_initial_messages(user_id, message)
        tools = tool_definitions()
        ctx = self._tool_ctx(user_id)

        for turn in range(MAX_TOOL_TURNS):
            response: LLMResponse = await self.llm.complete(
                f"ace-tool-loop-{turn}",
                messages,
                use_cache=False,
                tools=tools,
            )

            if not response.tool_calls:
                if response.content:
                    messages.append(ChatMessage(role="assistant", content=response.content))
                break

            # Build assistant message with tool_calls
            messages.append(ChatMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=[
                    {"id": t.id, "type": "function", "function": t.function}
                    for t in response.tool_calls
                ],
            ))

            for tc in response.tool_calls:
                func_name = tc.function["name"]
                func_args = tc.function["arguments"]

                # Emit tool_start
                yield json.dumps({
                    "type": "tool_start",
                    "toolCallId": tc.id,
                    "name": func_name,
                    "arguments": func_args,
                }, ensure_ascii=False)

                # Pre-execution safety check for shell commands
                if func_name == "shell_execute":
                    try:
                        args = json.loads(func_args) if func_args else {}
                        command = args.get("command", "")
                    except (json.JSONDecodeError, TypeError):
                        command = ""
                    danger = is_dangerous_command(command)
                    if danger:
                        register_approval(tc.id, command)
                        yield json.dumps({
                            "type": "approval",
                            "toolCallId": tc.id,
                            "command": command,
                            "reason": danger,
                        }, ensure_ascii=False)
                        await await_approval(tc.id).wait()
                        approved = approval_decision(tc.id)
                        cleanup_approval(tc.id)
                        if not approved:
                            yield json.dumps({
                                "type": "tool_result",
                                "toolCallId": tc.id,
                                "name": func_name,
                                "summary": "⚠️ Command execution denied by user",
                            }, ensure_ascii=False)
                            messages.append(ChatMessage(
                                role="tool",
                                content=json.dumps({"status": "denied", "command": command}, ensure_ascii=False),
                                tool_call_id=tc.id,
                            ))
                            continue

                # Execute tool
                try:
                    tool_result = await execute_tool(func_name, func_args, ctx)
                    parsed_result = json.loads(tool_result)
                except Exception as exc:
                    parsed_result = {"error": f"Tool execution failed: {exc}"}

                summary = self._tool_result_summary(func_name, parsed_result)
                yield json.dumps({
                    "type": "tool_result",
                    "toolCallId": tc.id,
                    "name": func_name,
                    "summary": summary,
                }, ensure_ascii=False)

                messages.append(ChatMessage(
                    role="tool",
                    content=json.dumps(parsed_result, ensure_ascii=False),
                    tool_call_id=tc.id,
                ))
        else:
            msg = "工具调用次数已达上限，基于已有信息回答。"
            messages.append(ChatMessage("assistant", msg))

        # Stream the final answer. Keep tool-call assistant messages paired with their
        # tool responses, otherwise providers like DeepSeek reject the request.
        final_messages = messages
        async for chunk in self.llm.stream(final_messages):
            yield json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)
        yield json.dumps({"type": "done"})

    async def _ace_reply(self, user_id: str, message: str) -> str:
        """Non-streaming Ace mode with tool calling."""
        messages = self._ace_initial_messages(user_id, message)
        tools = tool_definitions()
        ctx = self._tool_ctx(user_id)

        for turn in range(MAX_TOOL_TURNS):
            response = await self.llm.complete(
                f"ace-reply-tool-{turn}",
                messages,
                use_cache=False,
                tools=tools,
            )
            if not response.tool_calls:
                return response.content

            messages.append(ChatMessage(
                role="assistant",
                content=response.content or "",
                tool_calls=[
                    {"id": t.id, "type": "function", "function": t.function}
                    for t in response.tool_calls
                ],
            ))
            for tc in response.tool_calls:
                tool_result = await execute_tool(tc.function["name"], tc.function["arguments"], ctx)
                messages.append(ChatMessage(
                    role="tool",
                    content=tool_result,
                    tool_call_id=tc.id,
                ))
        return "工具调用次数已达上限，基于已有信息回答。"

    def _ace_initial_messages(self, user_id: str, message: str) -> list[ChatMessage]:
        """Build initial messages for Ace mode with tool-calling system prompt."""
        return [
            ChatMessage(
                "system",
                (
                    "你是一个研究助手 Ace Chat，拥有以下工具的完全使用权。当用户的问题需要查询信息或执行操作时，"
                    "你可以自主决定调用哪些工具、按什么顺序调用，并使用工具返回的结果来回答用户。\n\n"
                    "可用工具：\n"
                    "1. search_database — 在本地 SQLite 论文库中按关键词搜索论文\n"
                    "2. search_rag_database — 深入某篇论文的解析内容进行问答\n"
                    "3. web_search — 通过 Brave 搜索实时网络信息\n"
                    "4. arxiv_search — 直接搜索 arXiv 获取最新论文\n"
                    "5. add_to_favorites — 将论文收藏到指定文件夹（自动创建文件夹）\n"
                    "6. list_favorite_folders — 列出所有收藏文件夹\n"
                    "7. shell_execute — 执行安全的 shell 命令（危险命令需用户批准）\n\n"
                    "工作原则：\n"
                    "- 可以同时调用多个工具以加速（如同时搜索数据库和 arXiv）\n"
                    "- 多步任务可以连续调用不同工具（如先搜索→再收藏）\n"
                    "- 用中文回答，给出引用来源（论文的 arxivId 或网页 URL）\n"
                    "- 如果工具返回空结果，如实告知用户\n"
                    "- 不要编造工具没返回的信息"
                ),
            ),
            ChatMessage("user", message),
        ]

    # ------------------------------------------------------------------
    # Fallbacks
    # ------------------------------------------------------------------

    def _tool_result_summary(self, name: str, result: dict) -> str:
        """Generate a human-readable summary of a tool result for UI display."""
        if name == "search_database":
            total = result.get("total", 0)
            return f"找到 {total} 篇相关论文" if total else "未找到匹配论文"
        if name == "search_rag_database":
            count = result.get("total_chunks", 0)
            title = result.get("paper_title", "")
            return f"从「{title}」中找到 {count} 个相关片段" if title else f"找到 {count} 个相关片段"
        if name == "web_search":
            total = result.get("total", 0)
            return f"网络搜索到 {total} 条结果" if total else "网络搜索未找到结果"
        if name == "arxiv_search":
            total = result.get("total", 0)
            return f"arXiv 搜索到 {total} 篇论文" if total else "arXiv 未找到匹配论文"
        if name == "shell_execute":
            rc = result.get("return_code")
            if rc == 0:
                out = result.get("stdout", "")
                return f"命令执行成功 (exit 0)，输出 {len(out)} 字符"
            return f"命令执行失败 (exit {rc})"
        if name == "list_favorite_folders":
            folders = result.get("folders", [])
            return f"共 {len(folders)} 个收藏文件夹"
        if name == "add_to_favorites":
            added = result.get("added", 0)
            return f"已收藏 {added} 篇论文到「{result.get('folder_name', '')}」"
        return f"工具 {name} 执行完成"

    def _user_facing_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if exc.__class__.__name__ == "HTTPStatusError":
            return "模型服务请求失败，Ace Chat 本轮没有生成结果。已修正工具调用链后请重试；如果仍失败，请检查 LLM 接口配置。"
        if message:
            return f"生成失败：{message}"
        return "生成失败：外部服务连接异常"

    @staticmethod
    def approve_tool_call(tool_call_id: str, approved: bool) -> bool:
        """Resolve a pending approval. Called from the API endpoint."""
        from app.features.tools.registry import resolve_approval
        return resolve_approval(tool_call_id, approved)

    def _local_answer(self, user_id: str, message: str) -> str:
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
