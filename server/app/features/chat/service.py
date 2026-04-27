import json
import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date

from app.db.connection import transaction
from app.core.errors import AppError
from app.features.chat.agents import (
    AGENTS_BY_KEY,
    CLASSIFIER_SYSTEM_PROMPT,
    PAPER_ACE_AGENT_CHARTER,
    AgentSpec,
    IntentClassification,
    agent_catalog,
    fallback_intent_classification,
    parse_intent_classification,
    select_agents,
)
from app.features.chat.memory import AgentMemoryStore
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
_mission_queue_lock = asyncio.Lock()
PAPER_ACE_SCOPE = "paper_ace"


@dataclass
class AgentRunResult:
    agent: AgentSpec
    content: str
    tool_results: list[dict] = field(default_factory=list)


class ChatService:
    def __init__(self) -> None:
        self.llm = LLMClient()
        self.papers = PaperService()
        self.preferences = UserPreferenceService()
        self.search_tool = BraveSearchTool()
        self.arxiv_tool = ArxivTool()
        self.agent_memory = AgentMemoryStore()

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
        return {"deletedSessionId": session_id}

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
        attachment_paper_ids: list[int] | None = None,
        mode: str = "paper",
    ) -> dict:
        mode = self._normalize_mode(mode)
        ensure_user(user_id)
        self.preferences.update_from_text(user_id, message)
        stored_message = self._message_for_storage(message, attachment_paper_ids)
        with transaction() as connection:
            connection.execute(
                "INSERT INTO chat_messages(session_id, role, content, selection) VALUES(?, 'user', ?, ?)",
                (session_id, stored_message, selection),
            )
        session_history = self._recent_session_messages(session_id)

        answer = await self._paper_ace_reply(
            user_id,
            message,
            paper_id=paper_id,
            selection=selection,
            session_history=session_history,
            attachment_paper_ids=attachment_paper_ids,
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
        return {"answer": answer, "messages": self.messages(session_id)}

    # ------------------------------------------------------------------
    # Background missions
    # ------------------------------------------------------------------

    def submit_mission(
        self,
        user_id: str,
        session_id: str,
        message: str,
        paper_id: int | None = None,
        selection: str | None = None,
        attachment_paper_ids: list[int] | None = None,
        mode: str = "paper",
    ) -> dict:
        mode = self._normalize_mode(mode)
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
                (session_id, user_id, mode, message, paper_id, selection, json.dumps(attachment_paper_ids)),
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
        attachment_paper_ids: list[int] | None = None,
        mode: str = "paper",
    ) -> AsyncIterator[str]:
        mode = self._normalize_mode(mode)
        ensure_user(user_id)
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
            async for event in self._stream_paper_ace_ndjson(
                user_id,
                message,
                paper_id=paper_id,
                selection=selection,
                session_history=session_history,
                attachment_paper_ids=attachment_paper_ids,
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

    def _accumulate_text(self, event: str, text_chunks: list[str]) -> list[str]:
        """Extract text content from an NDJSON event for DB storage."""
        try:
            parsed = json.loads(event)
            if parsed.get("type") == "text" and parsed.get("content"):
                text_chunks.append(parsed["content"])
        except json.JSONDecodeError:
            pass
        return text_chunks

    def _chunk_text(self, text: str, size: int = 56) -> list[str]:
        if not text:
            return []
        return [text[index : index + size] for index in range(0, len(text), size)]

    # ------------------------------------------------------------------
    # Paper mode (uses RAG context — no tools)
    # ------------------------------------------------------------------

    async def _stream_paper_ndjson(
        self,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None = None,
    ) -> AsyncIterator[str]:
        """Paper mode — wraps LLM stream in NDJSON text events."""
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "")
        prompt = self._paper_user_prompt(message, paper_id, selection, context_chunks, attachment_paper_ids)
        messages = self._paper_conversation_messages(session_history, prompt)
        async for chunk in self.llm.stream(messages):
            yield json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)
        yield json.dumps({"type": "done"})

    async def _paper_reply(
        self,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "")
        prompt = self._paper_user_prompt(message, paper_id, selection, context_chunks, attachment_paper_ids)
        response = await self.llm.complete(
            "paper-chat",
            self._paper_conversation_messages(session_history, prompt),
            use_cache=False,
        )
        return response.content

    # ------------------------------------------------------------------
    # Paper Ace Paper — six-agent tool-calling entrypoint
    # ------------------------------------------------------------------

    def agents(self) -> list[dict]:
        return agent_catalog()

    async def _stream_paper_ace_ndjson(
        self,
        user_id: str,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None = None,
    ) -> AsyncIterator[str]:
        classification = await self._classify_intent(message, has_long_history=len(session_history) >= 12)
        selected_agents = select_agents(classification=classification)
        for agent in selected_agents:
            yield json.dumps(
                {
                    "type": "agent_start",
                    "agentKey": agent.key,
                    "agentName": agent.name,
                    "summary": agent.when_to_use,
                },
                ensure_ascii=False,
            )
        yield self._thinking_event(
            "classifier",
            "Intent classifier",
            f"Intent: {classification.primary_intent}; agents: {', '.join(agent.key for agent in selected_agents)}. {classification.rationale}",
        )

        candidate_agents = [agent for agent in selected_agents if agent.key in {"research", "inspiration", "suggestion"}]
        other_agents = [agent for agent in selected_agents if agent.key in {"summary", "tool_maker"}]
        tasks = [
            asyncio.create_task(
                self._run_candidate_agent(
                    agent,
                    user_id,
                    message,
                    paper_id=paper_id,
                    selection=selection,
                    session_history=session_history,
                    attachment_paper_ids=attachment_paper_ids,
                    classification=classification,
                )
            )
            for agent in candidate_agents
        ]

        for agent in other_agents:
            tasks.append(
                asyncio.create_task(
                    self._run_candidate_agent(
                        agent,
                        user_id,
                        message,
                        paper_id=paper_id,
                        selection=selection,
                        session_history=session_history,
                        attachment_paper_ids=attachment_paper_ids,
                        classification=classification,
                    )
                )
            )

        results: list[AgentRunResult] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            for tool_result in result.tool_results:
                yield json.dumps(
                    {
                        "type": "tool_start",
                        "toolCallId": tool_result["id"],
                        "name": tool_result["name"],
                        "arguments": tool_result["arguments"],
                    },
                    ensure_ascii=False,
                )
                yield json.dumps(
                    {
                        "type": "tool_result",
                        "toolCallId": tool_result["id"],
                        "name": tool_result["name"],
                        "summary": self._tool_result_summary(tool_result["name"], tool_result["result"]),
                    },
                    ensure_ascii=False,
                )
            yield self._thinking_event(result.agent.key, result.agent.name, result.content[:900])
            yield json.dumps(
                {
                    "type": "agent_result",
                    "agentKey": result.agent.key,
                    "agentName": result.agent.name,
                    "summary": "候选结果已生成，等待 Evaluation Agent 汇总。",
                },
                ensure_ascii=False,
            )

        evaluation_agent = AGENTS_BY_KEY["evaluation"]
        yield self._thinking_event("evaluation", evaluation_agent.name, "Checking candidate outputs for cited claims and composing final answer.")
        final_answer = await self._evaluate_agent_outputs(
            user_id,
            message,
            paper_id=paper_id,
            selection=selection,
            session_history=session_history,
            attachment_paper_ids=attachment_paper_ids,
            classification=classification,
            results=results,
        )
        output_by_agent = {result.agent.key: result.content for result in results}
        self.agent_memory.update_from_turn(user_id, message, final_answer, output_by_agent)
        yield json.dumps(
            {
                "type": "agent_result",
                "agentKey": evaluation_agent.key,
                "agentName": evaluation_agent.name,
                "summary": "已完成引用约束检查并生成最终回答。",
            },
            ensure_ascii=False,
        )
        for chunk in self._chunk_text(final_answer):
            yield json.dumps({"type": "text", "content": chunk}, ensure_ascii=False)
        yield json.dumps({"type": "done"})

    async def _paper_ace_reply(
        self,
        user_id: str,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        classification = await self._classify_intent(message, has_long_history=len(session_history) >= 12)
        selected_agents = select_agents(classification=classification)
        tasks = [
            self._run_candidate_agent(
                agent,
                user_id,
                message,
                paper_id=paper_id,
                selection=selection,
                session_history=session_history,
                attachment_paper_ids=attachment_paper_ids,
                classification=classification,
            )
            for agent in selected_agents
            if agent.key != "evaluation"
        ]
        results = await asyncio.gather(*tasks) if tasks else []
        final_answer = await self._evaluate_agent_outputs(
            user_id,
            message,
            paper_id=paper_id,
            selection=selection,
            session_history=session_history,
            attachment_paper_ids=attachment_paper_ids,
            classification=classification,
            results=list(results),
        )
        self.agent_memory.update_from_turn(user_id, message, final_answer, {result.agent.key: result.content for result in results})
        return final_answer

    async def _classify_intent(self, message: str, has_long_history: bool = False) -> IntentClassification:
        if not self.llm.settings.llm_api_key:
            return fallback_intent_classification(message, has_long_history=has_long_history)
        response = await self.llm.complete(
            "paper-ace-intent-classifier",
            [
                ChatMessage("system", CLASSIFIER_SYSTEM_PROMPT),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "message": message,
                            "has_long_history": has_long_history,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            use_cache=False,
        )
        return parse_intent_classification(response.content) or fallback_intent_classification(
            message,
            has_long_history=has_long_history,
        )

    async def _run_candidate_agent(
        self,
        agent: AgentSpec,
        user_id: str,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None,
        classification: IntentClassification,
    ) -> AgentRunResult:
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "") if paper_id else []
        memories = self.agent_memory.get_many(user_id, [agent.key])
        messages = [
            ChatMessage("system", self._candidate_system_prompt(agent)),
            ChatMessage(
                "system",
                (
                    f"Runtime date: {date.today().isoformat()}.\n"
                    f"Current user id: {user_id}.\n"
                    f"Intent classification: {classification.primary_intent}; {', '.join(classification.intents)}.\n"
                    f"Agent memory:\n{memories[agent.key].brief() if agent.key in memories else 'No dedicated memory.'}"
                ),
            ),
            *self._history_to_chat_messages(session_history[:-1]),
            ChatMessage(
                "user",
                self._paper_ace_user_prompt(
                    message,
                    paper_id=paper_id,
                    selection=selection,
                    context_chunks=context_chunks,
                    attachment_paper_ids=attachment_paper_ids,
                ),
            ),
        ]
        tools = self._candidate_tool_definitions(agent)
        tool_results: list[dict] = []
        ctx = self._tool_ctx(user_id)

        for turn in range(3):
            response = await self.llm.complete(
                f"paper-ace-{agent.key}-{turn}",
                messages,
                use_cache=False,
                tools=tools,
            )
            if not response.tool_calls:
                content = response.content or "No candidate output generated."
                return AgentRunResult(agent=agent, content=content, tool_results=tool_results)

            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=[
                        {"id": t.id, "type": "function", "function": t.function}
                        for t in response.tool_calls
                    ],
                )
            )
            for tc in response.tool_calls:
                try:
                    raw_result = await execute_tool(tc.function["name"], tc.function["arguments"], ctx)
                    parsed_result = json.loads(raw_result)
                except Exception as exc:
                    parsed_result = {"error": f"Tool execution failed: {exc}"}
                tool_results.append(
                    {
                        "id": f"{agent.key}-{tc.id}",
                        "name": tc.function["name"],
                        "arguments": tc.function["arguments"],
                        "result": parsed_result,
                    }
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=json.dumps(parsed_result, ensure_ascii=False),
                        tool_call_id=tc.id,
                    )
                )

        return AgentRunResult(
            agent=agent,
            content="Agent stopped after tool-call limit; use available tool results cautiously.",
            tool_results=tool_results,
        )

    def _candidate_system_prompt(self, agent: AgentSpec) -> str:
        shared = (
            f"You are the {agent.name}. {agent.purpose}\n"
            "Return a concise candidate answer for the Evaluation Agent, not the final user answer.\n"
            "Every factual claim must include a source marker in square brackets, such as [paper_id=12], [arXiv:2401.12345], or [https://example.com].\n"
            "If evidence is missing, write 'unsupported' instead of guessing."
        )
        if agent.key == "research":
            return shared + "\nUse database/RAG/arXiv/web tools when needed. Prefer source-backed findings over broad advice."
        if agent.key == "suggestion":
            return shared + "\nUse memory about recommendation feedback. Explain why recommendations match or conflict with past preferences."
        if agent.key == "inspiration":
            return shared + "\nGenerate creative but evidence-aware angles; separate facts from speculative ideas."
        if agent.key == "summary":
            return shared + "\nCompress prior context and candidate evidence into a short working summary."
        if agent.key == "tool_maker":
            return shared + "\nOnly suggest reusable tools when repetition or precision clearly justifies them."
        return shared

    def _candidate_tool_definitions(self, agent: AgentSpec) -> list[dict]:
        names_by_agent = {
            "research": {"search_database", "search_rag_database", "web_search", "arxiv_search"},
            "suggestion": {"search_database", "list_favorite_folders"},
            "inspiration": {"search_database", "search_rag_database", "arxiv_search"},
            "summary": set(),
            "tool_maker": set(),
        }
        names = names_by_agent.get(agent.key, set())
        return [tool for tool in tool_definitions() if tool["function"]["name"] in names]

    async def _evaluate_agent_outputs(
        self,
        user_id: str,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None,
        classification: IntentClassification,
        results: list[AgentRunResult],
    ) -> str:
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "") if paper_id else []
        source_refs = self._available_source_refs(paper_id, attachment_paper_ids, context_chunks, results)
        candidate_block = "\n\n".join(
            f"## {result.agent.name}\n{result.content}\nTool refs: {', '.join(self._refs_from_tool_results(result.tool_results)) or 'none'}"
            for result in results
        ) or "No candidate agents produced output."
        messages = [
            ChatMessage("system", PAPER_ACE_AGENT_CHARTER),
            ChatMessage(
                "system",
                (
                    "You are the Evaluation Agent. Produce the final answer for the user.\n"
                    "Hard citation rule: every key factual claim must include an explicit bracketed source reference from the available refs.\n"
                    "Reject, flag, or downgrade claims that lack a matching tool result, paper ID, arXiv ID, or URL.\n"
                    "Speculative ideas may be labeled as hypotheses, but supporting factual premises still need refs.\n"
                    f"Available source refs: {', '.join(source_refs) or 'none'}.\n"
                    f"Runtime date: {date.today().isoformat()}.\n"
                    f"Intent classification: {classification.primary_intent}; {', '.join(classification.intents)}."
                ),
            ),
            *self._history_to_chat_messages(session_history[:-1]),
            ChatMessage(
                "user",
                (
                    f"User request: {message}\n"
                    f"Selection: {selection[:1200] if selection else 'none'}\n"
                    f"Candidate outputs:\n{candidate_block}\n\n"
                    "Write the final answer in the user's language. Include a short '引用检查' note if any important candidate claim was unsupported."
                )[:20000],
            ),
        ]
        response = await self.llm.complete("paper-ace-evaluation", messages, use_cache=False)
        final_answer = response.content
        citation_report = self._citation_report(final_answer, source_refs)
        if citation_report:
            final_answer = f"{final_answer.rstrip()}\n\n引用检查：{citation_report}"
        return final_answer

    def _available_source_refs(
        self,
        paper_id: int | None,
        attachment_paper_ids: list[int] | None,
        context_chunks: list[str],
        results: list[AgentRunResult],
    ) -> list[str]:
        refs: list[str] = []
        if paper_id:
            refs.append(f"paper_id={paper_id}")
        for attached_id in attachment_paper_ids or []:
            refs.append(f"paper_id={attached_id}")
        text = "\n".join(context_chunks + [result.content for result in results])
        refs.extend(f"arXiv:{match}" for match in re.findall(r"arXiv:\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", text, flags=re.I))
        refs.extend(re.findall(r"https?://[^\s)\]]+", text))
        for result in results:
            refs.extend(self._refs_from_tool_results(result.tool_results))
        return sorted(set(refs))

    def _refs_from_tool_results(self, tool_results: list[dict]) -> list[str]:
        refs: list[str] = []
        for item in tool_results:
            result = item.get("result", {})
            if "paper_id" in result:
                refs.append(f"paper_id={result['paper_id']}")
            for paper in result.get("results", []) if isinstance(result.get("results"), list) else []:
                if paper.get("id"):
                    refs.append(f"paper_id={paper['id']}")
                if paper.get("arxivId"):
                    refs.append(f"arXiv:{paper['arxivId']}")
                if paper.get("absUrl"):
                    refs.append(paper["absUrl"])
                if paper.get("url"):
                    refs.append(paper["url"])
            for paper in result.get("folders", []) if isinstance(result.get("folders"), list) else []:
                if paper.get("id"):
                    refs.append(f"favorite_folder={paper['id']}")
        return refs

    def _citation_report(self, answer: str, source_refs: list[str]) -> str:
        factual_lines = [
            line.strip()
            for line in answer.splitlines()
            if len(line.strip()) > 30 and not line.lstrip().startswith(("引用检查", "References", "来源"))
        ]
        if not factual_lines:
            return ""
        unsupported = [line for line in factual_lines if not self._line_has_known_ref(line, source_refs)]
        if not unsupported:
            return ""
        sample = "；".join(line[:90] for line in unsupported[:3])
        return f"发现 {len(unsupported)} 条可能缺少显式来源绑定的陈述，已保留但需要人工核验：{sample}"

    def _line_has_known_ref(self, line: str, source_refs: list[str]) -> bool:
        markers = re.findall(r"\[([^\]]+)\]", line)
        if not markers:
            return False
        if not source_refs:
            return False
        normalized_refs = {ref.lower() for ref in source_refs}
        for marker in markers:
            normalized_marker = marker.lower()
            if any(ref in normalized_marker or normalized_marker in ref for ref in normalized_refs):
                return True
        return False

    def _thinking_event(self, agent_key: str, agent_name: str, content: str) -> str:
        return json.dumps(
            {
                "type": "thinking",
                "agentKey": agent_key,
                "agentName": agent_name,
                "content": content,
            },
            ensure_ascii=False,
        )

    def _paper_ace_initial_messages(
        self,
        user_id: str,
        message: str,
        paper_id: int | None,
        selection: str | None,
        session_history: list[dict],
        attachment_paper_ids: list[int] | None = None,
        selected_agent_keys: list[str] | None = None,
    ) -> list[ChatMessage]:
        context_chunks = self.papers.retrieve_context(paper_id, message if message.strip() else selection or "") if paper_id else []
        return [
            ChatMessage("system", PAPER_ACE_AGENT_CHARTER),
            ChatMessage(
                "system",
                (
                    f"Runtime date: {date.today().isoformat()}.\n"
                    f"Current user id: {user_id}.\n"
                    f"Active agent keys for this turn: {', '.join(selected_agent_keys or [])}.\n"
                    "Maintain the exact stable charter above as reusable context; put volatile date, user, paper, and request data here."
                ),
            ),
            *self._history_to_chat_messages(session_history[:-1]),
            ChatMessage(
                "user",
                self._paper_ace_user_prompt(
                    message,
                    paper_id=paper_id,
                    selection=selection,
                    context_chunks=context_chunks,
                    attachment_paper_ids=attachment_paper_ids,
                ),
            ),
        ]

    def _paper_ace_user_prompt(
        self,
        message: str,
        paper_id: int | None,
        selection: str | None,
        context_chunks: list[str],
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        focused_paper = ""
        if paper_id:
            try:
                paper = self.papers.get_papers_by_ids([paper_id])[0]
                focused_paper = (
                    "当前界面焦点论文：\n"
                    f"- DB paper_id={paper['id']} | {paper['title']} | arXiv: {paper.get('arxivId', '')}\n"
                    f"- 摘要: {(paper.get('aiSummary') or paper.get('abstract') or '')[:900]}\n"
                )
            except IndexError:
                focused_paper = f"当前界面焦点论文 ID：{paper_id}（数据库未返回详情）\n"
        selection_block = f"\n用户当前选中的原文片段：\n{selection[:3000]}\n" if selection else ""
        attachment_block = self._format_attachment_block(attachment_paper_ids, exclude_paper_id=paper_id)
        retrieval_block = "\n---\n".join(context_chunks) if context_chunks else "无当前焦点论文 RAG 片段；如需要证据，请调用 search_database/search_rag_database/arxiv_search/web_search。"
        return (
            f"用户当前请求：{message}\n"
            f"{focused_paper}"
            f"{selection_block}"
            f"{attachment_block}\n"
            "当前焦点论文 RAG 预取片段：\n"
            f"{retrieval_block}"
        )[:16000]

    # ------------------------------------------------------------------
    # Ace mode — legacy tool-calling agent kept for existing callers
    # ------------------------------------------------------------------

    async def _stream_ace_ndjson(
        self,
        user_id: str,
        message: str,
        session_history: list[dict],
        active_paper_id: int | None = None,
        attachment_paper_ids: list[int] | None = None,
    ) -> AsyncIterator[str]:
        """Ace mode tool-calling loop with NDJSON streaming events.

        Yields JSON lines: tool_start, approval, tool_result, text, done.
        """
        messages = self._ace_initial_messages(
            user_id,
            message,
            session_history=session_history,
            active_paper_id=active_paper_id,
            attachment_paper_ids=attachment_paper_ids,
        )
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

    async def _ace_reply(
        self,
        user_id: str,
        message: str,
        session_history: list[dict],
        active_paper_id: int | None = None,
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        """Non-streaming Ace mode with tool calling."""
        messages = self._ace_initial_messages(
            user_id,
            message,
            session_history=session_history,
            active_paper_id=active_paper_id,
            attachment_paper_ids=attachment_paper_ids,
        )
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

    def _ace_initial_messages(
        self,
        user_id: str,
        message: str,
        session_history: list[dict],
        active_paper_id: int | None = None,
        attachment_paper_ids: list[int] | None = None,
    ) -> list[ChatMessage]:
        """Build initial messages for Ace mode with tool-calling system prompt."""
        today = date.today().isoformat()
        messages = [
            ChatMessage(
                "system",
                (
                    f"你是研究工作台里的 Ace Chat。今天的日期是 {today}。\n"
                    "你拥有以下工具的完全使用权。当用户的问题需要查询信息、检索论文、联网查询或执行操作时，"
                    "你要主动决定是否调用工具，并基于工具结果回答。\n\n"
                    "可用工具：\n"
                    "1. search_database — 在本地 SQLite 论文库中按关键词搜索论文\n"
                    "2. search_rag_database — 深入某篇论文的解析内容进行问答\n"
                    "3. web_search — 通过 Brave 搜索实时网络信息\n"
                    "4. arxiv_search — 直接搜索 arXiv 获取最新论文\n"
                    "5. add_to_favorites — 将论文收藏到指定文件夹（自动创建文件夹）\n"
                    "6. list_favorite_folders — 列出所有收藏文件夹\n"
                    "7. shell_execute — 执行安全的 shell 命令（危险命令需用户批准）\n\n"
                    "工作原则：\n"
                    "- 会话是连续的。优先继承当前会话里已经确认的偏好、约束和上下文，不要把每轮都当成全新问题。\n"
                    "- 当用户提到“今天、当前、实时、最新、recent、latest”时，保留这些时间约束，必要时用确切日期补全为 "
                    f"{today}，不要擅自改成别的年份或日期。\n"
                    "- 做网页搜索时优先使用直接、贴近用户原话的查询词，不要为了“优化”而篡改核心条件。\n"
                    "- 如果当前界面有用户选中的论文或显式附加的论文，把它们当作高优先级上下文。\n"
                    "- 可以同时调用多个工具以加速，多步任务也可以连续调用不同工具。\n"
                    "- 用中文回答，结论后面附来源（论文 arxivId / 标题，或网页 URL）。\n"
                    "- 如果工具返回空结果、配置缺失或时间信息不确定，要明确说出来。\n"
                    "- 不要编造工具没返回的信息。"
                ),
            ),
        ]
        messages.extend(self._history_to_chat_messages(session_history[:-1]))
        messages.append(ChatMessage("user", self._ace_user_prompt(message, active_paper_id, attachment_paper_ids)))
        return messages

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
            return "模型服务请求失败，Paper Ace Paper 本轮没有生成结果。请重试；如果仍失败，请检查 LLM 接口配置。"
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

    def _history_to_chat_messages(self, rows: list[dict]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for row in rows:
            role = row.get("role")
            content = (row.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append(ChatMessage(role=role, content=content[:6000]))
        return messages

    def _attachment_papers(self, paper_ids: list[int] | None, exclude_paper_id: int | None = None) -> list[dict]:
        if not paper_ids:
            return []
        filtered = [paper_id for paper_id in paper_ids if paper_id != exclude_paper_id]
        return self.papers.get_papers_by_ids(filtered[:6])

    def _format_attachment_block(self, paper_ids: list[int] | None, exclude_paper_id: int | None = None) -> str:
        papers = self._attachment_papers(paper_ids, exclude_paper_id=exclude_paper_id)
        if not papers:
            return ""
        lines = ["用户附加了这些论文，请优先将它们作为额外上下文："]
        for index, paper in enumerate(papers, start=1):
            authors = ", ".join((paper.get("authors") or [])[:4])
            summary = (paper.get("aiSummary") or paper.get("abstract") or "").replace("\n", " ").strip()
            lines.append(
                f"{index}. [{paper['id']}] {paper['title']} (arXiv: {paper.get('arxivId', '')})"
                + (f" | Authors: {authors}" if authors else "")
                + (f"\n   摘要: {summary[:400]}" if summary else "")
            )
        return "\n".join(lines)

    def _paper_user_prompt(
        self,
        message: str,
        paper_id: int | None,
        selection: str | None,
        context_chunks: list[str],
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        selection_block = f"\n用户当前选中的原文片段：\n{selection[:3000]}" if selection else ""
        attachment_block = self._format_attachment_block(attachment_paper_ids, exclude_paper_id=paper_id)
        retrieval_block = "\n---\n".join(context_chunks) if context_chunks else "无额外检索片段。"
        return (
            f"今天日期：{date.today().isoformat()}\n"
            f"当前论文 ID：{paper_id or '未知'}\n"
            f"用户问题：{message}\n"
            f"{selection_block}\n"
            f"{attachment_block}\n"
            "当前论文检索上下文：\n"
            f"{retrieval_block}"
        )[:14000]

    def _paper_conversation_messages(self, session_history: list[dict], current_prompt: str) -> list[ChatMessage]:
        messages = [
            ChatMessage(
                "system",
                (
                    f"你是 Paper Chat。今天的日期是 {date.today().isoformat()}。\n"
                    "你要延续当前会话，不要忽略同一会话里前面的问答。"
                    "优先基于当前论文的检索片段回答；如果用户附加了其他论文，把它们视为次级参考。"
                    "用中文回答；不确定时明确说明；引用时优先写 arXiv 编号或论文标题。"
                ),
            )
        ]
        messages.extend(self._history_to_chat_messages(session_history[:-1]))
        messages.append(ChatMessage("user", current_prompt))
        return messages

    def _ace_user_prompt(
        self,
        message: str,
        active_paper_id: int | None = None,
        attachment_paper_ids: list[int] | None = None,
    ) -> str:
        active_paper_block = ""
        if active_paper_id:
            try:
                active_paper = self.papers.get_papers_by_ids([active_paper_id])[0]
                active_paper_block = (
                    "当前界面焦点论文：\n"
                    f"- [{active_paper['id']}] {active_paper['title']} (arXiv: {active_paper.get('arxivId', '')})\n"
                )
            except IndexError:
                active_paper_block = ""
        attachment_block = self._format_attachment_block(attachment_paper_ids, exclude_paper_id=active_paper_id)
        return (
            f"用户当前请求：{message}\n"
            f"{active_paper_block}"
            f"{attachment_block}"
        )[:12000]

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
        attachment_block = self._format_attachment_block(attachment_paper_ids)
        if not attachment_block:
            return message
        return f"{message}\n\n{attachment_block}"

    def _normalize_mode(self, mode: str | None) -> str:
        if mode in {"paper", "ace", PAPER_ACE_SCOPE}:
            return PAPER_ACE_SCOPE
        return PAPER_ACE_SCOPE
