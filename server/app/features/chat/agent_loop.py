"""Reusable agent tool-calling loop extracted from PaperAceWorkflowEngine.

This separates the harness (loop control, retry, budget, observability)
from business logic (prompts, tools, conversation building).
"""
import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.errors import AppError
from app.features.chat.agents import AgentSpec, get_registered_agent
from app.features.chat.conversation import ChatConversationBuilder
from app.features.chat.memory_system import MemoryBundle
from app.features.chat.observability import WorkflowTrace
from app.features.chat.runtime import (
    estimate_cost_usd,
    estimate_tokens,
    get_chat_runtime_settings,
    retry_async,
    today_key,
    user_facing_error,
)
from app.features.chat.workflow_store import ChatWorkflowStore
from app.features.tools.llm import ChatMessage, LLMClient
from app.features.tools.registry import ToolContext, execute_tool, tool_definitions


@dataclass
class AgentLoopConfig:
    """Configuration for a single agent loop execution."""
    agent: AgentSpec
    workflow_id: str
    user_id: str
    session_id: str
    message: str
    paper_id: int | None
    selection: str | None
    attachment_paper_ids: list[int]
    injection_note: str
    memory_bundle: MemoryBundle
    context_chunks: list[str]
    session_history: list[dict]
    classification: Any


@dataclass
class AgentLoopResult:
    """Result of running an agent loop."""
    content: str
    tool_results: list[dict] = field(default_factory=list)
    prompt_version: str = ""
    attempt_count: int = 0
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    model: str = ""
    status: str = "success"
    error_message: str | None = None


class BudgetGuard:
    """Centralized budget enforcement. Call check_token() before LLM calls,
    check_tool() before tool calls."""

    def __init__(self, store: ChatWorkflowStore, runtime: Any, user_id: str) -> None:
        self._store = store
        self._runtime = runtime
        self._user_id = user_id

    def check_token(self, messages: list[ChatMessage]) -> None:
        estimate = estimate_tokens(*(msg.content for msg in messages))
        usage = self._store.daily_usage(self._user_id, today_key())
        if usage["total_tokens"] + estimate > self._runtime.daily_user_token_budget:
            raise AppError("Daily token budget exceeded", 429, "chat_token_budget_exceeded")

    def check_tool(self) -> None:
        usage = self._store.daily_usage(self._user_id, today_key())
        if usage["tool_calls"] + 1 > self._runtime.daily_user_tool_budget:
            raise AppError("Daily tool budget exceeded", 429, "chat_tool_budget_exceeded")


class AgentLoop:
    """Reusable agent tool-calling loop. Separates the harness from business logic."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        papers: Any,
        preferences: Any,
        search_tool: Any,
        arxiv_tool: Any,
        agent_memory: Any,
        daily_rag: Any,
        builder: ChatConversationBuilder,
        prompts: Any,
        store: ChatWorkflowStore,
        runtime: Any,
        emit_fn: Any | None = None,
    ) -> None:
        self.llm = llm
        self.papers = papers
        self.preferences = preferences
        self.search_tool = search_tool
        self.arxiv_tool = arxiv_tool
        self.agent_memory = agent_memory
        self.daily_rag = daily_rag
        self.builder = builder
        self.prompts = prompts
        self.store = store
        self.runtime = runtime
        self._emit_fn = emit_fn

    async def run(
        self,
        config: AgentLoopConfig,
        trace: WorkflowTrace,
        budget: BudgetGuard,
    ) -> AgentLoopResult:
        """Execute the agent tool-calling loop. Returns AgentLoopResult."""
        registered = get_registered_agent(config.agent.key)
        memories = self.agent_memory.get_many(config.user_id, [config.agent.key])
        prompt_version = "/".join(
            part
            for part in [
                self.prompts.version("candidate_base"),
                self.prompts.version(registered.prompt_key) if registered.prompt_key else "",
            ]
            if part
        )
        run_id = self.store.start_agent_run(
            config.workflow_id,
            agent_key=config.agent.key,
            agent_name=config.agent.name,
            phase=config.agent.phase,
            prompt_version=prompt_version,
        )
        started = time.perf_counter()
        tool_results: list[dict] = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        attempts = 0
        tool_call_count = 0
        status = "success"
        content = "No candidate output generated."
        error_message: str | None = None

        history_limit = 6 if config.memory_bundle.working_summary or config.memory_bundle.session.summary else 12
        messages = self._build_messages(config, registered, memories, history_limit)
        allowed_tool_names = set(config.agent.tools)
        tools = [tool for tool in tool_definitions() if tool["function"]["name"] in allowed_tool_names]
        ctx = self._tool_ctx(config.user_id)

        try:
            async with asyncio.timeout(self.runtime.agent_timeout_seconds):
                for turn in range(self.runtime.agent_max_tool_turns):
                    budget.check_token(messages)
                    response = await retry_async(
                        f"candidate-{config.agent.key}-{turn}",
                        lambda: self.llm.complete(
                            f"paper-ace-{config.agent.key}-{turn}",
                            messages,
                            use_cache=False,
                            tools=tools,
                            timeout_seconds=self.runtime.agent_timeout_seconds,
                        ),
                        attempts=self.runtime.llm_retry_attempts,
                        base_delay=self.runtime.llm_retry_backoff_seconds,
                    )
                    attempts += 1
                    p_tok, c_tok, t_tok = self._resolve_usage(response, messages, response.content)
                    prompt_tokens += p_tok
                    completion_tokens += c_tok
                    total_tokens += t_tok
                    if not response.tool_calls:
                        content = response.content or content
                        break
                    if tool_call_count + len(response.tool_calls) > self.runtime.max_tool_calls_per_agent:
                        status = "degraded"
                        content = "Agent stopped after tool quota; use available tool results cautiously."
                        break
                    messages.append(
                        ChatMessage(
                            role="assistant",
                            content=response.content or "",
                            tool_calls=[
                                {"id": tc.id, "type": "function", "function": tc.function}
                                for tc in response.tool_calls
                            ],
                        )
                    )
                    for tool_call in response.tool_calls:
                        parsed_result = await self._execute_tool(
                            config, trace, budget, allowed_tool_names,
                            f"{config.agent.key}-{tool_call.id}",
                            tool_call.function["name"],
                            tool_call.function["arguments"],
                            ctx,
                        )
                        tool_call_count += 1
                        tool_results.append({
                            "id": f"{config.agent.key}-{tool_call.id}",
                            "name": tool_call.function["name"],
                            "arguments": tool_call.function["arguments"],
                            "result": parsed_result,
                        })
                        messages.append(
                            ChatMessage(
                                role="tool",
                                content=json.dumps(parsed_result, ensure_ascii=False),
                                tool_call_id=tool_call.id,
                            )
                        )
                else:
                    status = "degraded"
                    content = "Agent stopped after tool-call limit; use available tool results cautiously."
        except AppError:
            raise
        except Exception as exc:
            status = "failed"
            error_message = user_facing_error(exc)
            content = f"unsupported\n\n{error_message}"

        estimated_cost = estimate_cost_usd(prompt_tokens, completion_tokens)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.store.finish_agent_run(
            run_id,
            status=status,
            attempt_count=attempts,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            tool_call_count=tool_call_count,
            metadata={"model": self.llm.settings.llm_model, "tool_results": len(tool_results)},
            error_message=error_message,
        )
        trace.totals.add_usage(prompt_tokens, completion_tokens, total_tokens, estimated_cost)
        self.store.increment_daily_usage(
            config.user_id, today_key(),
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, estimated_cost_usd=estimated_cost,
        )
        trace.log(
            "chat_agent_completed",
            orchestration="langgraph",
            agent_key=config.agent.key,
            status=status, duration_ms=duration_ms,
            total_tokens=total_tokens, tool_call_count=tool_call_count,
            estimated_cost_usd=estimated_cost,
        )
        return AgentLoopResult(
            content=content, tool_results=tool_results,
            prompt_version=prompt_version, attempt_count=attempts,
            duration_ms=duration_ms, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            model=self.llm.settings.llm_model,
            status=status, error_message=error_message,
        )

    def _build_messages(self, config: AgentLoopConfig, registered, memories, history_limit) -> list[ChatMessage]:
        from app.features.chat.agents import PAPER_ACE_AGENT_CHARTER
        return [
            ChatMessage("system", PAPER_ACE_AGENT_CHARTER),
            ChatMessage("system", registered.system_prompt()),
            ChatMessage("system", self.builder.memory_system_prompt(config.memory_bundle.prompt_block())),
            ChatMessage(
                "system",
                (
                    f"Runtime date: {date.today().isoformat()}.\n"
                    f"Current user id: {config.user_id}.\n"
                    f"Intent classification: {config.classification.primary_intent}; {', '.join(config.classification.intents)}.\n"
                    f"Agent memory:\n{memories[config.agent.key].brief() if config.agent.key in memories else 'No dedicated memory.'}\n"
                    f"Security note: {config.injection_note}"
                ),
            ),
            *self.builder.history_to_chat_messages(config.session_history[:-1], limit=history_limit),
            ChatMessage(
                "user",
                self.builder.paper_ace_user_prompt(
                    config.message,
                    paper_id=config.paper_id,
                    selection=config.selection,
                    context_chunks=config.context_chunks,
                    attachment_paper_ids=config.attachment_paper_ids,
                ),
            ),
        ]

    async def _execute_tool(
        self, config: AgentLoopConfig, trace: WorkflowTrace, budget: BudgetGuard,
        allowed_tool_names: set[str], tool_call_id: str, name: str, arguments: str, ctx: ToolContext,
    ) -> dict:
        if name not in allowed_tool_names:
            return {"error": f"Tool '{name}' is not allowed for agent '{config.agent.key}'"}
        budget.check_tool()
        await self._emit(config.workflow_id, {"type": "tool_start", "toolCallId": tool_call_id, "name": name, "arguments": arguments})
        started = time.perf_counter()
        try:
            raw_result = await asyncio.wait_for(
                execute_tool(name, arguments, ctx),
                timeout=self.runtime.tool_timeout_seconds,
            )
            parsed_result = json.loads(raw_result)
            status = "error" if parsed_result.get("error") else "success"
            error_message = parsed_result.get("error")
        except AppError:
            raise
        except Exception as exc:
            parsed_result = {"error": f"Tool execution failed: {exc}"}
            status = "error"
            error_message = str(exc)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.store.record_tool_run(
            config.workflow_id, agent_key=config.agent.key,
            tool_call_id=tool_call_id, tool_name=name, status=status,
            duration_ms=duration_ms, arguments_json=arguments,
            result_json=parsed_result, error_message=error_message,
        )
        trace.totals.add_tool_call()
        self.store.increment_daily_usage(config.user_id, today_key(), tool_calls=1)
        trace.log("chat_tool_completed", orchestration="langgraph", agent_key=config.agent.key, tool_name=name, status=status, duration_ms=duration_ms)
        await self._emit(config.workflow_id, {"type": "tool_result", "toolCallId": tool_call_id, "name": name, "summary": _tool_result_summary(name, parsed_result)})
        return parsed_result

    def _tool_ctx(self, user_id: str) -> ToolContext:
        return ToolContext(
            user_id=user_id, paper_service=self.papers,
            user_preferences=self.preferences, brave_search=self.search_tool,
            arxiv_tool=self.arxiv_tool, daily_rag=self.daily_rag,
        )

    def _resolve_usage(self, response, messages, content) -> tuple[int, int, int]:
        prompt_tokens = response.prompt_tokens or estimate_tokens(*(msg.content for msg in messages))
        completion_tokens = response.completion_tokens or estimate_tokens(content)
        total_tokens = response.total_tokens or (prompt_tokens + completion_tokens)
        return prompt_tokens, completion_tokens, total_tokens

    async def _emit(self, workflow_id: str, event: dict) -> None:
        if self._emit_fn:
            await self._emit_fn(workflow_id, event)


def _tool_result_summary(name: str, result: dict) -> str:
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
    if name == "list_favorite_folders":
        folders = result.get("folders", [])
        return f"共 {len(folders)} 个收藏文件夹"
    if name == "add_to_favorites":
        added = result.get("added", 0)
        return f"已收藏 {added} 篇论文到「{result.get('folder_name', '')}」"
    return f"工具 {name} 执行完成"
