import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.core.errors import AppError
from app.features.chat.agent_loop import AgentLoop, AgentLoopConfig, BudgetGuard
from app.features.chat.agents import (
    PAPER_ACE_AGENT_CHARTER,
    AgentSpec,
    ExecutionPlan,
    IntentClassification,
    build_execution_plan,
    fallback_intent_classification,
    parse_intent_classification,
)
from app.features.chat.conversation import (
    ChatConversationBuilder,
    available_source_refs,
    citation_report,
    refs_from_tool_results,
)
from app.features.chat.memory_system import ConversationMemoryManager, MemoryBundle
from app.features.chat.observability import WorkflowTrace
from app.features.chat.persistence import SQLiteStore
from app.features.chat.prompts import get_prompt_store
from app.features.chat.runtime import (
    assess_prompt_injection,
    estimate_cost_usd,
    estimate_tokens,
    get_chat_runtime_settings,
    retry_async,
    today_key,
    user_facing_error,
)
from app.features.chat.workflow_store import ChatWorkflowStore
from app.features.tools.llm import ChatMessage, LLMClient, LLMResponse

EmitFn = Callable[[dict], Awaitable[None]]


@dataclass
class WorkflowRequest:
    user_id: str
    session_id: str
    message: str
    paper_id: int | None = None
    selection: str | None = None
    session_history: list[dict] = field(default_factory=list)
    attachment_paper_ids: list[int] = field(default_factory=list)
    mode: str = "paper_ace"


@dataclass
class AgentRunResult:
    agent: AgentSpec
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


@dataclass(frozen=True)
class WorkflowRuntimeContext:
    user_id: str
    session_id: str
    mode: str


class WorkflowGraphState(TypedDict, total=False):
    request: WorkflowRequest
    workflow_id: str
    trace: WorkflowTrace
    injection_note: str
    classification: IntentClassification
    plan: ExecutionPlan
    prompt_versions: dict[str, str]
    ordered_agents: list[AgentSpec]
    batch_index: int
    memory_bundle: MemoryBundle
    history_summary: str
    final_answer: str
    context_chunks: list[str]


class PaperAceWorkflowEngine:
    def __init__(
        self,
        *,
        llm: LLMClient,
        papers: Any,
        preferences: Any,
        search_tool: Any,
        arxiv_tool: Any,
        agent_memory: Any,
        daily_rag: Any = None,
    ) -> None:
        self.llm = llm
        self.papers = papers
        self.preferences = preferences
        self.search_tool = search_tool
        self.arxiv_tool = arxiv_tool
        self.agent_memory = agent_memory
        self.daily_rag = daily_rag
        self.prompts = get_prompt_store()
        self.runtime = get_chat_runtime_settings()
        self.store = ChatWorkflowStore()
        self.memory_store = SQLiteStore()
        self.builder = ChatConversationBuilder(papers)
        self.memory_manager = ConversationMemoryManager(llm)
        self._emitters: dict[str, EmitFn] = {}
        self._candidate_results: dict[str, list[AgentRunResult]] = {}
        self._evaluation_results: dict[str, AgentRunResult] = {}
        self.agent_loop = AgentLoop(
            llm=self.llm,
            papers=self.papers,
            preferences=self.preferences,
            search_tool=self.search_tool,
            arxiv_tool=self.arxiv_tool,
            agent_memory=self.agent_memory,
            daily_rag=self.daily_rag,
            builder=self.builder,
            prompts=self.prompts,
            store=self.store,
            runtime=self.runtime,
            emit_fn=self._emit_event_wrapper,
        )
        self.graph = self._build_graph()

    async def run(self, request: WorkflowRequest) -> str:
        async def noop(_: dict) -> None:
            return None

        return await self._execute(request, noop)

    async def stream(self, request: WorkflowRequest):
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        error: Exception | None = None

        async def emit(event: dict) -> None:
            await queue.put(event)

        async def runner() -> None:
            nonlocal error
            try:
                await self._execute(request, emit)
            except Exception as exc:
                error = exc
            finally:
                await queue.put(None)

        task = asyncio.create_task(runner())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False)
        await task
        if error:
            raise error

    def _build_graph(self):
        graph = StateGraph(WorkflowGraphState, context_schema=WorkflowRuntimeContext)
        graph.add_node("classify", self._classify_node)
        graph.add_node("hydrate_memory", self._hydrate_memory_node)
        graph.add_node("summarize_context", self._summarize_context_node)
        graph.add_node("respond", self._respond_node)
        graph.add_node("finalize", self._finalize_node)
        graph.add_edge(START, "classify")
        graph.add_edge("classify", "hydrate_memory")
        graph.add_conditional_edges(
            "hydrate_memory",
            self._route_after_hydrate_memory,
            {"summarize_context": "summarize_context", "respond": "respond"},
        )
        graph.add_conditional_edges(
            "summarize_context",
            self._route_after_summary,
            {"respond": "respond"},
        )
        graph.add_edge("respond", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(store=self.memory_store)

    async def _execute(self, request: WorkflowRequest, emit: EmitFn) -> str:
        workflow_id = self.store.create_workflow_run(request.user_id, request.session_id, request.mode, request.message)
        trace = WorkflowTrace(workflow_id=workflow_id, user_id=request.user_id, session_id=request.session_id, mode=request.mode)
        injection = assess_prompt_injection(request.message, request.selection or "")
        initial_state: WorkflowGraphState = {
            "request": request,
            "workflow_id": workflow_id,
            "trace": trace,
            "injection_note": injection.system_note,
            "batch_index": 0,
            "memory_bundle": MemoryBundle(),
            "history_summary": "",
            "final_answer": "",
        }
        self._emitters[workflow_id] = emit
        self._candidate_results[workflow_id] = []
        try:
            async with asyncio.timeout(self.runtime.workflow_timeout_seconds):
                final_state = await self.graph.ainvoke(
                    initial_state,
                    config={
                        "recursion_limit": 32,
                        "configurable": {"thread_id": request.session_id, "checkpoint_ns": request.mode},
                    },
                    context=WorkflowRuntimeContext(
                        user_id=request.user_id,
                        session_id=request.session_id,
                        mode=request.mode,
                    ),
                )
            return final_state.get("final_answer", "")
        except Exception as exc:
            self.store.finish_workflow_run(
                workflow_id,
                status="failed",
                metrics=trace.totals.as_dict(),
                error_message=self._user_facing_error(exc),
            )
            trace.log("chat_workflow_failed", error=self._user_facing_error(exc), **trace.totals.as_dict())
            raise
        finally:
            self._emitters.pop(workflow_id, None)
            self._candidate_results.pop(workflow_id, None)
            self._evaluation_results.pop(workflow_id, None)

    async def _classify_node(self, state: WorkflowGraphState) -> dict:
        request = state["request"]
        trace = state["trace"]
        workflow_id = state["workflow_id"]

        classification = await self._classify_intent(request, trace)
        plan = build_execution_plan(classification)
        prompt_versions = {
            "agent_charter": self.prompts.version("paper_ace_agent_charter"),
            "classifier": self.prompts.version("intent_classifier_system"),
            "candidate_base": self.prompts.version("candidate_base"),
            "evaluation": self.prompts.version("evaluation_system"),
        }
        self.store.mark_workflow_context(
            workflow_id,
            classification={
                "primary_intent": classification.primary_intent,
                "intents": list(classification.intents),
                "agent_keys": list(classification.agent_keys),
                "confidence": classification.confidence,
                "rationale": classification.rationale,
            },
            prompt_versions=prompt_versions,
        )

        ordered_agents = [agent for batch in plan.parallel_batches for agent in batch] + [plan.evaluation_agent]
        for agent in ordered_agents:
            await self._emit_event(
                workflow_id,
                {
                    "type": "agent_start",
                    "agentKey": agent.key,
                    "agentName": agent.name,
                    "summary": agent.when_to_use,
                }
            )
        await self._emit_event(
            workflow_id,
            {
                "type": "thinking",
                "agentKey": "classifier",
                "agentName": "Intent classifier",
                "content": f"Intent: {classification.primary_intent}; agents: {', '.join(agent.key for agent in ordered_agents)}. {classification.rationale}",
            }
        )
        trace.log(
            "chat_workflow_started",
            orchestration="langgraph",
            classification=classification.primary_intent,
            agent_keys=list(classification.agent_keys),
        )
        return {
            "classification": classification,
            "plan": plan,
            "prompt_versions": prompt_versions,
            "ordered_agents": ordered_agents,
            "batch_index": 0,
        }

    async def _hydrate_memory_node(self, state: WorkflowGraphState, runtime: Runtime[WorkflowRuntimeContext]) -> dict:
        request = state["request"]
        workflow_id = state["workflow_id"]
        bundle = self.memory_manager.load_bundle(runtime.store, request.user_id, request.session_id, request.message)
        recall_count = len(bundle.episodes)
        await self._emit_event(
            workflow_id,
            {
                "type": "thinking",
                "agentKey": "memory",
                "agentName": "Memory Manager",
                "content": f"Loaded session memory, profile memory, and {recall_count} recalled episodes.",
            },
        )
        return {"memory_bundle": bundle}

    def _route_after_hydrate_memory(self, state: WorkflowGraphState) -> str:
        request = state["request"]
        bundle = state.get("memory_bundle", MemoryBundle())
        if self.memory_manager.should_summarize_history(request.session_history, bundle.session.summary):
            return "summarize_context"
        return "respond"

    async def _summarize_context_node(self, state: WorkflowGraphState) -> dict:
        request = state["request"]
        workflow_id = state["workflow_id"]
        bundle = state.get("memory_bundle", MemoryBundle())

        # If we already have a working_summary from the previous turn's persist_turn, reuse it
        if bundle.working_summary:
            await self._emit_event(
                workflow_id,
                {
                    "type": "thinking",
                    "agentKey": "summary",
                    "agentName": "Summary Agent",
                    "content": "Reusing previous turn's working memory summary.",
                },
            )
            return {"memory_bundle": bundle, "history_summary": bundle.working_summary}

        # Only call LLM if no existing summary
        summary = await self.memory_manager.summarize_history(request.session_history[:-1], bundle.session.summary)
        bundle.working_summary = summary
        await self._emit_event(
            workflow_id,
            {
                "type": "thinking",
                "agentKey": "summary",
                "agentName": "Summary Agent",
                "content": "Long history compressed into working memory for downstream agents.",
            },
        )
        return {"memory_bundle": bundle, "history_summary": summary}

    def _route_after_summary(self, state: WorkflowGraphState) -> str:
        _ = state
        return "respond"

    async def _respond_node(self, state: WorkflowGraphState) -> dict:
        workflow_id = state["workflow_id"]
        plan = state["plan"]
        trace = state["trace"]
        request = state["request"]
        classification = state["classification"]
        memory_bundle = state.get("memory_bundle", MemoryBundle())
        context_chunks = self.papers.retrieve_context(
            request.paper_id,
            request.message if request.message.strip() else request.selection or "",
        ) if request.paper_id else []
        all_results: list[AgentRunResult] = []

        for batch_index, batch in enumerate(plan.parallel_batches):
            trace.log("chat_candidate_batch_enter", batch_index=batch_index, batch_total=len(plan.parallel_batches))
            batch_results = await self._run_batch(
                workflow_id=workflow_id,
                batch=batch,
                request=request,
                classification=classification,
                injection_note=state["injection_note"],
                trace=trace,
                memory_bundle=memory_bundle,
                context_chunks=context_chunks,
            )
            all_results.extend(batch_results)
            trace.log("chat_candidate_batch_exit", batch_index=batch_index, produced=len(batch_results))

        self._candidate_results[workflow_id] = all_results
        trace.log("chat_evaluate_enter", candidate_results=len(all_results))
        await self._emit_event(
            workflow_id,
            {
                "type": "thinking",
                "agentKey": plan.evaluation_agent.key,
                "agentName": plan.evaluation_agent.name,
                "content": "Checking candidate outputs for cited claims and composing final answer.",
            },
        )
        evaluation_result = await self._evaluate(
            workflow_id=workflow_id,
            agent=plan.evaluation_agent,
            request=request,
            classification=classification,
            results=all_results,
            injection_note=state["injection_note"],
            trace=trace,
            memory_bundle=memory_bundle,
            context_chunks=context_chunks,
        )
        self._evaluation_results[workflow_id] = evaluation_result
        return {"final_answer": evaluation_result.content, "context_chunks": context_chunks}

    async def _finalize_node(self, state: WorkflowGraphState, runtime: Runtime[WorkflowRuntimeContext]) -> dict:
        request = state["request"]
        trace = state["trace"]
        workflow_id = state["workflow_id"]
        results = list(self._candidate_results.get(workflow_id, []))
        evaluation_result = self._evaluation_results[workflow_id]
        final_answer = state.get("final_answer", "")

        output_by_agent = {result.agent.key: result.content for result in results}
        self.agent_memory.update_from_turn(request.user_id, request.message, final_answer, output_by_agent)
        context_chunks = state.get("context_chunks", [])
        source_refs = available_source_refs(request.paper_id, request.attachment_paper_ids, context_chunks, results)
        await self.memory_manager.persist_turn(
            store=runtime.store,
            user_id=request.user_id,
            session_id=request.session_id,
            message=request.message,
            final_answer=final_answer,
            session_history=request.session_history,
            existing_bundle=state.get("memory_bundle", MemoryBundle()),
            source_refs=source_refs,
        )
        await self._emit_event(
            workflow_id,
            {
                "type": "agent_result",
                "agentKey": evaluation_result.agent.key,
                "agentName": evaluation_result.agent.name,
                "summary": "已完成引用约束检查并生成最终回答。",
            }
        )
        for chunk in self._chunk_text(final_answer):
            await self._emit_event(workflow_id, {"type": "text", "content": chunk})
        await self._emit_event(workflow_id, {"type": "done"})
        self.store.finish_workflow_run(workflow_id, status="success", metrics=trace.totals.as_dict())
        trace.log("chat_workflow_completed", orchestration="langgraph", **trace.totals.as_dict())
        return {"final_answer": final_answer}

    async def _classify_intent(self, request: WorkflowRequest, trace: WorkflowTrace) -> IntentClassification:
        if not self.llm.settings.llm_api_key:
            return fallback_intent_classification(request.message, has_long_history=len(request.session_history) >= 12)

        messages = [
            ChatMessage("system", self.prompts.render("intent_classifier_system")),
            ChatMessage(
                "user",
                json.dumps(
                    {
                        "message": request.message,
                        "has_long_history": len(request.session_history) >= 12,
                    },
                    ensure_ascii=False,
                ),
            ),
        ]
        budget = BudgetGuard(self.store, self.runtime, request.user_id)
        budget.check_token(messages)
        response = await retry_async(
            "intent-classifier",
            lambda: self.llm.complete(
                "paper-ace-intent-classifier",
                messages,
                use_cache=False,
                timeout_seconds=self.runtime.agent_timeout_seconds,
            ),
            attempts=self.runtime.llm_retry_attempts,
            base_delay=self.runtime.llm_retry_backoff_seconds,
        )
        prompt_tokens, completion_tokens, total_tokens = self._resolve_usage(response, messages, response.content)
        estimated_cost = estimate_cost_usd(prompt_tokens, completion_tokens)
        trace.totals.add_usage(prompt_tokens, completion_tokens, total_tokens, estimated_cost)
        self.store.increment_daily_usage(
            request.user_id,
            today_key(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
        )
        return parse_intent_classification(response.content) or fallback_intent_classification(
            request.message,
            has_long_history=len(request.session_history) >= 12,
        )

    async def _run_batch(
        self,
        *,
        workflow_id: str,
        batch: tuple[AgentSpec, ...],
        request: WorkflowRequest,
        classification: IntentClassification,
        injection_note: str,
        trace: WorkflowTrace,
        memory_bundle: MemoryBundle,
        context_chunks: list[str],
    ) -> list[AgentRunResult]:
        semaphore = asyncio.Semaphore(self.runtime.parallel_agent_limit)

        async def run_one(agent: AgentSpec) -> AgentRunResult:
            async with semaphore:
                return await self._run_candidate_agent(
                    workflow_id=workflow_id,
                    agent=agent,
                    request=request,
                    classification=classification,
                    injection_note=injection_note,
                    trace=trace,
                    memory_bundle=memory_bundle,
                    context_chunks=context_chunks,
                )

        tasks = [asyncio.create_task(run_one(agent)) for agent in batch]
        results: list[AgentRunResult] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            await self._emit_event(
                workflow_id,
                {
                    "type": "thinking",
                    "agentKey": result.agent.key,
                    "agentName": result.agent.name,
                    "content": result.content[:900],
                }
            )
            await self._emit_event(
                workflow_id,
                {
                    "type": "agent_result",
                    "agentKey": result.agent.key,
                    "agentName": result.agent.name,
                    "summary": "候选结果已生成，等待 Evaluation Agent 汇总。",
                }
            )
        return results

    async def _run_candidate_agent(
        self,
        *,
        workflow_id: str,
        agent: AgentSpec,
        request: WorkflowRequest,
        classification: IntentClassification,
        injection_note: str,
        trace: WorkflowTrace,
        memory_bundle: MemoryBundle,
        context_chunks: list[str],
    ) -> AgentRunResult:
        budget = BudgetGuard(self.store, self.runtime, request.user_id)
        loop_result = await self.agent_loop.run(
            AgentLoopConfig(
                agent=agent,
                workflow_id=workflow_id,
                user_id=request.user_id,
                session_id=request.session_id,
                message=request.message,
                paper_id=request.paper_id,
                selection=request.selection,
                attachment_paper_ids=request.attachment_paper_ids,
                injection_note=injection_note,
                memory_bundle=memory_bundle,
                context_chunks=context_chunks,
                session_history=request.session_history,
                classification=classification,
            ),
            trace,
            budget,
        )
        return AgentRunResult(
            agent=agent,
            content=loop_result.content,
            tool_results=loop_result.tool_results,
            prompt_version=loop_result.prompt_version,
            attempt_count=loop_result.attempt_count,
            duration_ms=loop_result.duration_ms,
            prompt_tokens=loop_result.prompt_tokens,
            completion_tokens=loop_result.completion_tokens,
            total_tokens=loop_result.total_tokens,
            estimated_cost_usd=loop_result.estimated_cost_usd,
            model=loop_result.model,
            status=loop_result.status,
            error_message=loop_result.error_message,
        )

    async def _evaluate(
        self,
        *,
        workflow_id: str,
        agent: AgentSpec,
        request: WorkflowRequest,
        classification: IntentClassification,
        results: list[AgentRunResult],
        injection_note: str,
        trace: WorkflowTrace,
        memory_bundle: MemoryBundle,
        context_chunks: list[str],
    ) -> AgentRunResult:
        source_refs = available_source_refs(request.paper_id, request.attachment_paper_ids, context_chunks, results)
        candidate_block = "\n\n".join(
            f"## {result.agent.name}\n{result.content}\nTool refs: {', '.join(refs_from_tool_results(result.tool_results)) or 'none'}"
            for result in results
        ) or "No candidate agents produced output."
        prompt_version = self.prompts.version("evaluation_system")
        run_id = self.store.start_agent_run(
            workflow_id,
            agent_key=agent.key,
            agent_name=agent.name,
            phase=agent.phase,
            prompt_version=prompt_version,
        )
        started = time.perf_counter()
        messages = [
            ChatMessage("system", PAPER_ACE_AGENT_CHARTER),
            ChatMessage(
                "system",
                self.prompts.render(
                    "evaluation_system",
                    available_source_refs=", ".join(source_refs) or "none",
                    runtime_date=date.today().isoformat(),
                    primary_intent=classification.primary_intent,
                    all_intents=", ".join(classification.intents),
                    security_note=injection_note,
                ),
            ),
            ChatMessage("system", self.builder.memory_system_prompt(memory_bundle.prompt_block())),
            *self.builder.history_to_chat_messages(request.session_history[:-1], limit=8),
            ChatMessage(
                "user",
                (
                    f"User request: {request.message}\n"
                    f"Selection: {request.selection[:1200] if request.selection else 'none'}\n"
                    f"Candidate outputs:\n{candidate_block}\n\n"
                    "Write the final answer in the user's language. Include a short '引用检查' note if any important candidate claim was unsupported."
                )[:20000],
            ),
        ]
        budget = BudgetGuard(self.store, self.runtime, request.user_id)
        budget.check_token(messages)
        response = await retry_async(
            "evaluation-agent",
            lambda: self.llm.complete(
                "paper-ace-evaluation",
                messages,
                use_cache=False,
                timeout_seconds=self.runtime.agent_timeout_seconds,
            ),
            attempts=self.runtime.llm_retry_attempts,
            base_delay=self.runtime.llm_retry_backoff_seconds,
        )
        prompt_tokens, completion_tokens, total_tokens = self._resolve_usage(response, messages, response.content)
        final_answer = response.content
        citation_note = citation_report(final_answer, source_refs)
        if citation_note:
            final_answer = f"{final_answer.rstrip()}\n\n引用检查：{citation_note}"
        estimated_cost = estimate_cost_usd(prompt_tokens, completion_tokens)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.store.finish_agent_run(
            run_id,
            status="success",
            attempt_count=1,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            tool_call_count=0,
            metadata={"model": response.model or self.llm.settings.llm_model},
        )
        trace.totals.add_usage(prompt_tokens, completion_tokens, total_tokens, estimated_cost)
        self.store.increment_daily_usage(
            request.user_id,
            today_key(),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
        )
        trace.log(
            "chat_agent_completed",
            orchestration="langgraph",
            agent_key=agent.key,
            status="success",
            duration_ms=duration_ms,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
        )
        return AgentRunResult(
            agent=agent,
            content=final_answer,
            prompt_version=prompt_version,
            attempt_count=1,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            model=response.model or self.llm.settings.llm_model,
        )

    def _resolve_usage(self, response: LLMResponse, messages: list[ChatMessage], content: str) -> tuple[int, int, int]:
        prompt_tokens = response.prompt_tokens or estimate_tokens(*(message.content for message in messages))
        completion_tokens = response.completion_tokens or estimate_tokens(content)
        total_tokens = response.total_tokens or (prompt_tokens + completion_tokens)
        return prompt_tokens, completion_tokens, total_tokens

    def _chunk_text(self, text: str, size: int = 56) -> list[str]:
        if not text:
            return []
        return [text[index : index + size] for index in range(0, len(text), size)]

    def _user_facing_error(self, exc: Exception) -> str:
        return user_facing_error(exc)

    async def _emit_event_wrapper(self, workflow_id: str, event: dict) -> None:
        await self._emit_event(workflow_id, event)

    async def _emit_event(self, workflow_id: str, event: dict) -> None:
        emit = self._emitters.get(workflow_id)
        if emit:
            await emit(event)
