import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.errors import AppError
from app.features.chat.agents import (
    PAPER_ACE_AGENT_CHARTER,
    AgentSpec,
    ExecutionPlan,
    IntentClassification,
    build_execution_plan,
    fallback_intent_classification,
    get_registered_agent,
    parse_intent_classification,
)
from app.features.chat.conversation import (
    ChatConversationBuilder,
    available_source_refs,
    citation_report,
    refs_from_tool_results,
)
from app.features.chat.observability import WorkflowTrace
from app.features.chat.prompts import get_prompt_store
from app.features.chat.runtime import (
    assess_prompt_injection,
    estimate_cost_usd,
    estimate_tokens,
    get_chat_runtime_settings,
    retry_async,
    today_key,
)
from app.features.chat.workflow_store import ChatWorkflowStore
from app.features.tools.llm import ChatMessage, LLMClient, LLMResponse
from app.features.tools.registry import ToolContext, execute_tool, tool_definitions

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


class WorkflowGraphState(TypedDict, total=False):
    request: WorkflowRequest
    emit: EmitFn
    workflow_id: str
    trace: WorkflowTrace
    injection_note: str
    classification: IntentClassification
    plan: ExecutionPlan
    prompt_versions: dict[str, str]
    ordered_agents: list[AgentSpec]
    batch_index: int
    results: list[AgentRunResult]
    evaluation_result: AgentRunResult
    final_answer: str


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
        self.builder = ChatConversationBuilder(papers)
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
        graph = StateGraph(WorkflowGraphState)
        graph.add_node("classify", self._classify_node)
        graph.add_node("candidate_batch", self._candidate_batch_node)
        graph.add_node("evaluate", self._evaluate_node)
        graph.add_node("finalize", self._finalize_node)
        graph.add_edge(START, "classify")
        graph.add_conditional_edges(
            "classify",
            self._route_after_classify,
            {"candidate_batch": "candidate_batch", "evaluate": "evaluate"},
        )
        graph.add_conditional_edges(
            "candidate_batch",
            self._route_after_candidate_batch,
            {"candidate_batch": "candidate_batch", "evaluate": "evaluate"},
        )
        graph.add_edge("evaluate", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile()

    async def _execute(self, request: WorkflowRequest, emit: EmitFn) -> str:
        workflow_id = self.store.create_workflow_run(request.user_id, request.session_id, request.mode, request.message)
        trace = WorkflowTrace(workflow_id=workflow_id, user_id=request.user_id, session_id=request.session_id, mode=request.mode)
        injection = assess_prompt_injection(request.message, request.selection or "")
        initial_state: WorkflowGraphState = {
            "request": request,
            "emit": emit,
            "workflow_id": workflow_id,
            "trace": trace,
            "injection_note": injection.system_note,
            "batch_index": 0,
            "results": [],
            "final_answer": "",
        }
        try:
            async with asyncio.timeout(self.runtime.workflow_timeout_seconds):
                final_state = await self.graph.ainvoke(
                    initial_state,
                    config={
                        "recursion_limit": 32,
                        "configurable": {"thread_id": workflow_id},
                    },
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

    async def _classify_node(self, state: WorkflowGraphState) -> dict:
        request = state["request"]
        emit = state["emit"]
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
            await emit(
                {
                    "type": "agent_start",
                    "agentKey": agent.key,
                    "agentName": agent.name,
                    "summary": agent.when_to_use,
                }
            )
        await emit(
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
            "results": [],
        }

    def _route_after_classify(self, state: WorkflowGraphState) -> str:
        plan = state["plan"]
        return "candidate_batch" if plan.parallel_batches else "evaluate"

    async def _candidate_batch_node(self, state: WorkflowGraphState) -> dict:
        plan = state["plan"]
        batch_index = state.get("batch_index", 0)
        if batch_index >= len(plan.parallel_batches):
            return {"batch_index": batch_index}
        batch_results = await self._run_batch(
            workflow_id=state["workflow_id"],
            batch=plan.parallel_batches[batch_index],
            request=state["request"],
            classification=state["classification"],
            injection_note=state["injection_note"],
            trace=state["trace"],
            emit=state["emit"],
        )
        results = list(state.get("results", []))
        results.extend(batch_results)
        return {"results": results, "batch_index": batch_index + 1}

    def _route_after_candidate_batch(self, state: WorkflowGraphState) -> str:
        plan = state["plan"]
        batch_index = state.get("batch_index", 0)
        return "candidate_batch" if batch_index < len(plan.parallel_batches) else "evaluate"

    async def _evaluate_node(self, state: WorkflowGraphState) -> dict:
        plan = state["plan"]
        emit = state["emit"]
        await emit(
            {
                "type": "thinking",
                "agentKey": plan.evaluation_agent.key,
                "agentName": plan.evaluation_agent.name,
                "content": "Checking candidate outputs for cited claims and composing final answer.",
            }
        )
        evaluation_result = await self._evaluate(
            workflow_id=state["workflow_id"],
            agent=plan.evaluation_agent,
            request=state["request"],
            classification=state["classification"],
            results=state.get("results", []),
            injection_note=state["injection_note"],
            trace=state["trace"],
        )
        return {
            "evaluation_result": evaluation_result,
            "final_answer": evaluation_result.content,
        }

    async def _finalize_node(self, state: WorkflowGraphState) -> dict:
        request = state["request"]
        emit = state["emit"]
        trace = state["trace"]
        workflow_id = state["workflow_id"]
        results = state.get("results", [])
        evaluation_result = state["evaluation_result"]
        final_answer = state.get("final_answer", "")

        output_by_agent = {result.agent.key: result.content for result in results}
        self.agent_memory.update_from_turn(request.user_id, request.message, final_answer, output_by_agent)
        await emit(
            {
                "type": "agent_result",
                "agentKey": evaluation_result.agent.key,
                "agentName": evaluation_result.agent.name,
                "summary": "已完成引用约束检查并生成最终回答。",
            }
        )
        for chunk in self._chunk_text(final_answer):
            await emit({"type": "text", "content": chunk})
        await emit({"type": "done"})
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
        self._assert_token_budget(request.user_id, messages)
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
        emit: EmitFn,
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
                    emit=emit,
                )

        tasks = [asyncio.create_task(run_one(agent)) for agent in batch]
        results: list[AgentRunResult] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            results.append(result)
            await emit(
                {
                    "type": "thinking",
                    "agentKey": result.agent.key,
                    "agentName": result.agent.name,
                    "content": result.content[:900],
                }
            )
            await emit(
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
        emit: EmitFn,
    ) -> AgentRunResult:
        registered = get_registered_agent(agent.key)
        context_chunks = self.papers.retrieve_context(
            request.paper_id,
            request.message if request.message.strip() else request.selection or "",
        ) if request.paper_id else []
        memories = self.agent_memory.get_many(request.user_id, [agent.key])
        prompt_version = "/".join(
            part
            for part in [
                self.prompts.version("candidate_base"),
                self.prompts.version(registered.prompt_key) if registered.prompt_key else "",
            ]
            if part
        )
        run_id = self.store.start_agent_run(
            workflow_id,
            agent_key=agent.key,
            agent_name=agent.name,
            phase=agent.phase,
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
        messages = [
            ChatMessage("system", PAPER_ACE_AGENT_CHARTER),
            ChatMessage("system", registered.system_prompt()),
            ChatMessage(
                "system",
                (
                    f"Runtime date: {date.today().isoformat()}.\n"
                    f"Current user id: {request.user_id}.\n"
                    f"Intent classification: {classification.primary_intent}; {', '.join(classification.intents)}.\n"
                    f"Agent memory:\n{memories[agent.key].brief() if agent.key in memories else 'No dedicated memory.'}\n"
                    f"Security note: {injection_note}"
                ),
            ),
            *self.builder.history_to_chat_messages(request.session_history[:-1]),
            ChatMessage(
                "user",
                self.builder.paper_ace_user_prompt(
                    request.message,
                    paper_id=request.paper_id,
                    selection=request.selection,
                    context_chunks=context_chunks,
                    attachment_paper_ids=request.attachment_paper_ids,
                ),
            ),
        ]
        allowed_tool_names = set(agent.tools)
        tools = [tool for tool in tool_definitions() if tool["function"]["name"] in allowed_tool_names]
        ctx = self._tool_ctx(request.user_id)

        try:
            async with asyncio.timeout(self.runtime.agent_timeout_seconds):
                for turn in range(self.runtime.agent_max_tool_turns):
                    self._assert_token_budget(request.user_id, messages)
                    response = await retry_async(
                        f"candidate-{agent.key}-{turn}",
                        lambda: self.llm.complete(
                            f"paper-ace-{agent.key}-{turn}",
                            messages,
                            use_cache=False,
                            tools=tools,
                            timeout_seconds=self.runtime.agent_timeout_seconds,
                        ),
                        attempts=self.runtime.llm_retry_attempts,
                        base_delay=self.runtime.llm_retry_backoff_seconds,
                    )
                    attempts += 1
                    used_prompt_tokens, used_completion_tokens, used_total_tokens = self._resolve_usage(
                        response,
                        messages,
                        response.content,
                    )
                    prompt_tokens += used_prompt_tokens
                    completion_tokens += used_completion_tokens
                    total_tokens += used_total_tokens
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
                                {"id": tool_call.id, "type": "function", "function": tool_call.function}
                                for tool_call in response.tool_calls
                            ],
                        )
                    )
                    for tool_call in response.tool_calls:
                        parsed_result = await self._execute_tool(
                            workflow_id=workflow_id,
                            agent=agent,
                            tool_call_id=f"{agent.key}-{tool_call.id}",
                            name=tool_call.function["name"],
                            arguments=tool_call.function["arguments"],
                            ctx=ctx,
                            request=request,
                            trace=trace,
                            emit=emit,
                        )
                        tool_call_count += 1
                        tool_results.append(
                            {
                                "id": f"{agent.key}-{tool_call.id}",
                                "name": tool_call.function["name"],
                                "arguments": tool_call.function["arguments"],
                                "result": parsed_result,
                            }
                        )
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
            error_message = self._user_facing_error(exc)
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
            status=status,
            duration_ms=duration_ms,
            total_tokens=total_tokens,
            tool_call_count=tool_call_count,
            estimated_cost_usd=estimated_cost,
        )
        return AgentRunResult(
            agent=agent,
            content=content,
            tool_results=tool_results,
            prompt_version=prompt_version,
            attempt_count=attempts,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost,
            model=self.llm.settings.llm_model,
            status=status,
            error_message=error_message,
        )

    async def _execute_tool(
        self,
        *,
        workflow_id: str,
        agent: AgentSpec,
        tool_call_id: str,
        name: str,
        arguments: str,
        ctx: ToolContext,
        request: WorkflowRequest,
        trace: WorkflowTrace,
        emit: EmitFn,
    ) -> dict:
        self._assert_tool_budget(request.user_id)
        await emit({"type": "tool_start", "toolCallId": tool_call_id, "name": name, "arguments": arguments})
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
            workflow_id,
            agent_key=agent.key,
            tool_call_id=tool_call_id,
            tool_name=name,
            status=status,
            duration_ms=duration_ms,
            arguments_json=arguments,
            result_json=parsed_result,
            error_message=error_message,
        )
        trace.totals.add_tool_call()
        self.store.increment_daily_usage(request.user_id, today_key(), tool_calls=1)
        trace.log(
            "chat_tool_completed",
            orchestration="langgraph",
            agent_key=agent.key,
            tool_name=name,
            status=status,
            duration_ms=duration_ms,
        )
        await emit(
            {
                "type": "tool_result",
                "toolCallId": tool_call_id,
                "name": name,
                "summary": self._tool_result_summary(name, parsed_result),
            }
        )
        return parsed_result

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
    ) -> AgentRunResult:
        context_chunks = self.papers.retrieve_context(
            request.paper_id,
            request.message if request.message.strip() else request.selection or "",
        ) if request.paper_id else []
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
            *self.builder.history_to_chat_messages(request.session_history[:-1]),
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
        self._assert_token_budget(request.user_id, messages)
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

    def _tool_ctx(self, user_id: str) -> ToolContext:
        return ToolContext(
            user_id=user_id,
            paper_service=self.papers,
            user_preferences=self.preferences,
            brave_search=self.search_tool,
            arxiv_tool=self.arxiv_tool,
            daily_rag=self.daily_rag,
        )

    def _assert_token_budget(self, user_id: str, messages: list[ChatMessage]) -> None:
        estimate = estimate_tokens(*(message.content for message in messages))
        usage = self.store.daily_usage(user_id, today_key())
        if usage["total_tokens"] + estimate > self.runtime.daily_user_token_budget:
            raise AppError("Daily token budget exceeded", 429, "chat_token_budget_exceeded")

    def _assert_tool_budget(self, user_id: str) -> None:
        usage = self.store.daily_usage(user_id, today_key())
        if usage["tool_calls"] + 1 > self.runtime.daily_user_tool_budget:
            raise AppError("Daily tool budget exceeded", 429, "chat_tool_budget_exceeded")

    def _resolve_usage(self, response: LLMResponse, messages: list[ChatMessage], content: str) -> tuple[int, int, int]:
        prompt_tokens = response.prompt_tokens or estimate_tokens(*(message.content for message in messages))
        completion_tokens = response.completion_tokens or estimate_tokens(content)
        total_tokens = response.total_tokens or (prompt_tokens + completion_tokens)
        return prompt_tokens, completion_tokens, total_tokens

    def _chunk_text(self, text: str, size: int = 56) -> list[str]:
        if not text:
            return []
        return [text[index : index + size] for index in range(0, len(text), size)]

    def _tool_result_summary(self, name: str, result: dict) -> str:
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

    def _user_facing_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if exc.__class__.__name__ == "HTTPStatusError":
            return "模型服务请求失败，Paper Ace Paper 本轮没有生成结果。请重试；如果仍失败，请检查 LLM 接口配置。"
        if isinstance(exc, TimeoutError):
            return "生成失败：本轮执行超时，请缩小问题范围后重试。"
        if message:
            return f"生成失败：{message}"
        return "生成失败：外部服务连接异常"
