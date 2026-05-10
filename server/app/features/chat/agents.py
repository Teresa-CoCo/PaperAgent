import json
import re
from dataclasses import dataclass, field

from app.features.chat.prompts import get_prompt_store


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    purpose: str
    when_to_use: str
    phase: str = "candidate"
    priority: int = 100
    parallel_group: str = "default"
    tools: tuple[str, ...] = field(default_factory=tuple)
    prompt_key: str = ""


@dataclass(frozen=True)
class IntentClassification:
    primary_intent: str
    intents: tuple[str, ...] = field(default_factory=tuple)
    agent_keys: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    rationale: str = ""


@dataclass(frozen=True)
class ExecutionPlan:
    parallel_batches: tuple[tuple[AgentSpec, ...], ...]
    evaluation_agent: AgentSpec


class BaseAgent:
    key = ""
    name = ""
    purpose = ""
    when_to_use = ""
    phase = "candidate"
    priority = 100
    parallel_group = "default"
    tools: tuple[str, ...] = tuple()
    prompt_key = ""

    def spec(self) -> AgentSpec:
        return AgentSpec(
            key=self.key,
            name=self.name,
            purpose=self.purpose,
            when_to_use=self.when_to_use,
            phase=self.phase,
            priority=self.priority,
            parallel_group=self.parallel_group,
            tools=self.tools,
            prompt_key=self.prompt_key,
        )

    def system_prompt(self) -> str:
        store = get_prompt_store()
        instruction = store.render(self.prompt_key) if self.prompt_key else ""
        return store.render(
            "candidate_base",
            agent_name=self.name,
            agent_purpose=self.purpose,
            agent_instruction=instruction,
        )


_AGENT_REGISTRY: dict[str, BaseAgent] = {}


def register_agent(*, key: str, tools: set[str] | tuple[str, ...], priority: int, phase: str = "candidate", parallel_group: str = "default"):
    def decorator(cls: type[BaseAgent]) -> type[BaseAgent]:
        instance = cls()
        instance.key = key
        instance.priority = priority
        instance.phase = phase
        instance.parallel_group = parallel_group
        instance.tools = tuple(sorted(tools))
        _AGENT_REGISTRY[key] = instance
        return cls

    return decorator


@register_agent(
    key="research",
    tools={"search_database", "search_rag_database", "search_daily_rag", "web_search", "arxiv_search"},
    priority=10,
    parallel_group="exploration",
)
class ResearchAgent(BaseAgent):
    name = "Research Agent"
    purpose = "Search local SQL papers, parsed RAG chunks, daily paper vector store, arXiv, and web sources for the user's research request."
    when_to_use = "Use for paper lookup, factual questions, literature search, daily paper semantic search, and any request needing external or database evidence."
    prompt_key = "agent_research"


@register_agent(key="summary", tools=set(), priority=40, parallel_group="synthesis")
class SummaryAgent(BaseAgent):
    name = "Summary Agent"
    purpose = "Compress long chat history, tool results, and agent outputs into short working memory."
    when_to_use = "Use when context is long, when multiple tool results need synthesis, or before the final response."
    prompt_key = "agent_summary"


@register_agent(
    key="inspiration",
    tools={"search_database", "search_rag_database", "search_daily_rag", "arxiv_search"},
    priority=20,
    parallel_group="exploration",
)
class InspirationAgent(BaseAgent):
    name = "Inspiration Agent"
    purpose = "Identify innovation points, hidden assumptions, and promising research angles."
    when_to_use = "Use when the user asks for ideas, novelty, future work, research gaps, or deeper directions."
    prompt_key = "agent_inspiration"


@register_agent(
    key="suggestion",
    tools={"search_database", "search_daily_rag", "list_favorite_folders"},
    priority=30,
    parallel_group="exploration",
)
class SuggestionAgent(BaseAgent):
    name = "Suggestion Agent"
    purpose = "Recommend papers and research directions aligned with the user's stated and inferred preferences."
    when_to_use = "Use for recommendations, reading lists, next papers to study, or personalized direction finding."
    prompt_key = "agent_suggestion"


@register_agent(key="tool_maker", tools=set(), priority=50, parallel_group="synthesis")
class ToolMakerAgent(BaseAgent):
    name = "Tool Maker Agent"
    purpose = "Decide whether a lightweight reusable tool or skill would materially improve the task."
    when_to_use = "Use sparingly. Prefer existing tools; only suggest or create tools when repetition or precision justifies it."
    prompt_key = "agent_tool_maker"


@register_agent(key="evaluation", tools=set(), priority=1000, phase="evaluation", parallel_group="evaluation")
class EvaluationAgent(BaseAgent):
    name = "Evaluation Agent"
    purpose = "Verify claims, require references, and flag uncertainty or missing evidence."
    when_to_use = "Use before final answers and whenever claims depend on tools, papers, or current facts."


INTENT_LABELS = {
    "research": "paper lookup, factual question, literature search, evidence gathering, local RAG, daily paper vector search, arXiv, or web search",
    "summary": "summarization or compression of long context/results",
    "inspiration": "novel ideas, innovation points, research gaps, future work, or creative directions",
    "suggestion": "recommendations, reading lists, next papers, or personalized direction finding",
    "tool_maker": "requests to create, adapt, or automate reusable tools/scripts/skills",
    "evaluation": "verification, source checking, citation review, uncertainty assessment",
}

PAPER_ACE_AGENTS: tuple[AgentSpec, ...] = tuple(
    agent.spec() for agent in sorted(_AGENT_REGISTRY.values(), key=lambda item: item.priority)
)
AGENTS_BY_KEY = {agent.key: agent for agent in PAPER_ACE_AGENTS}
PAPER_ACE_AGENT_CHARTER = get_prompt_store().render("paper_ace_agent_charter")
CLASSIFIER_SYSTEM_PROMPT = get_prompt_store().render("intent_classifier_system")


def get_registered_agent(key: str) -> BaseAgent:
    return _AGENT_REGISTRY[key]


def parse_intent_classification(raw: str) -> IntentClassification | None:
    text = raw.strip()
    if not text:
        return None
    if "```" in text:
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return normalize_intent_classification(payload)


def normalize_intent_classification(payload: dict) -> IntentClassification:
    agent_keys = [str(key) for key in payload.get("agent_keys", []) if str(key) in AGENTS_BY_KEY]
    intents = [str(intent) for intent in payload.get("intents", []) if str(intent) in INTENT_LABELS]
    primary = str(payload.get("primary_intent") or (intents[0] if intents else "research"))
    if primary not in INTENT_LABELS:
        primary = "research"
    if not agent_keys:
        agent_keys = [primary]
    if "research" not in agent_keys and primary in {"research", "evaluation"}:
        agent_keys.insert(0, "research")
    if "evaluation" not in agent_keys:
        agent_keys.append("evaluation")
    if not intents:
        intents = [key for key in agent_keys if key in INTENT_LABELS]
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return IntentClassification(
        primary_intent=primary,
        intents=tuple(_unique(intents)),
        agent_keys=tuple(_unique(agent_keys)),
        confidence=max(0.0, min(confidence, 1.0)),
        rationale=str(payload.get("rationale") or ""),
    )


def fallback_intent_classification(message: str, has_long_history: bool = False) -> IntentClassification:
    lowered = message.lower()
    intents: list[str] = ["research"]
    if has_long_history or any(token in lowered for token in ("summary", "summarize", "总结", "概括", "简短")):
        intents.append("summary")
    if any(token in lowered for token in ("idea", "inspire", "inspiration", "innovation", "novel", "future", "gap", "research on", "创新", "启发", "灵感", "方向", "不足")):
        intents.append("inspiration")
    if any(token in lowered for token in ("recommend", "suggest", "next", "reading", "feedback", "推荐", "建议", "下一篇", "偏好", "不喜欢", "喜欢")):
        intents.append("suggestion")
    if any(token in lowered for token in ("tool", "skill", "script", "automation", "自动化", "工具", "技能", "脚本")):
        intents.append("tool_maker")
    intents.append("evaluation")
    unique_intents = tuple(_unique(intents))
    return IntentClassification(
        primary_intent=unique_intents[0],
        intents=unique_intents,
        agent_keys=unique_intents,
        confidence=0.45,
        rationale="offline fallback classification",
    )


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def agent_catalog() -> list[dict]:
    return [
        {
            "key": agent.key,
            "name": agent.name,
            "purpose": agent.purpose,
            "whenToUse": agent.when_to_use,
            "phase": agent.phase,
            "priority": agent.priority,
            "tools": list(agent.tools),
        }
        for agent in PAPER_ACE_AGENTS
    ]


def select_agents(
    message: str = "",
    has_long_history: bool = False,
    classification: IntentClassification | None = None,
) -> list[AgentSpec]:
    if classification is None:
        classification = fallback_intent_classification(message, has_long_history=has_long_history)
    return [AGENTS_BY_KEY[key] for key in classification.agent_keys if key in AGENTS_BY_KEY]


def build_execution_plan(classification: IntentClassification) -> ExecutionPlan:
    selected = select_agents(classification=classification)
    evaluation_agent = next((agent for agent in selected if agent.phase == "evaluation"), AGENTS_BY_KEY["evaluation"])
    grouped: dict[str, list[AgentSpec]] = {}
    ordered_groups: list[str] = []
    for agent in sorted((item for item in selected if item.phase != "evaluation"), key=lambda item: item.priority):
        if agent.parallel_group not in grouped:
            grouped[agent.parallel_group] = []
            ordered_groups.append(agent.parallel_group)
        grouped[agent.parallel_group].append(agent)
    batches = tuple(tuple(grouped[group]) for group in ordered_groups)
    return ExecutionPlan(parallel_batches=batches, evaluation_agent=evaluation_agent)
