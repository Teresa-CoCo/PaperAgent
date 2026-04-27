import json
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    purpose: str
    when_to_use: str


@dataclass(frozen=True)
class IntentClassification:
    primary_intent: str
    intents: tuple[str, ...] = field(default_factory=tuple)
    agent_keys: tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    rationale: str = ""


PAPER_ACE_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="research",
        name="Research Agent",
        purpose="Search local SQL papers, parsed RAG chunks, arXiv, and web sources for the user's research request.",
        when_to_use="Use for paper lookup, factual questions, literature search, and any request needing external or database evidence.",
    ),
    AgentSpec(
        key="summary",
        name="Summary Agent",
        purpose="Compress long chat history, tool results, and agent outputs into short working memory.",
        when_to_use="Use when context is long, when multiple tool results need synthesis, or before the final response.",
    ),
    AgentSpec(
        key="inspiration",
        name="Inspiration Agent",
        purpose="Identify innovation points, hidden assumptions, and promising research angles.",
        when_to_use="Use when the user asks for ideas, novelty, future work, research gaps, or deeper directions.",
    ),
    AgentSpec(
        key="suggestion",
        name="Suggestion Agent",
        purpose="Recommend papers and research directions aligned with the user's stated and inferred preferences.",
        when_to_use="Use for recommendations, reading lists, next papers to study, or personalized direction finding.",
    ),
    AgentSpec(
        key="tool_maker",
        name="Tool Maker Agent",
        purpose="Decide whether a lightweight reusable tool or skill would materially improve the task.",
        when_to_use="Use sparingly. Prefer existing tools; only suggest or create tools when repetition or precision justifies it.",
    ),
    AgentSpec(
        key="evaluation",
        name="Evaluation Agent",
        purpose="Verify claims, require references, and flag uncertainty or missing evidence.",
        when_to_use="Use before final answers and whenever claims depend on tools, papers, or current facts.",
    ),
)


PAPER_ACE_AGENT_CHARTER = """You are Paper Ace Paper, a multi-agent research workspace.

Stable agent team:
1. Research Agent: search all available local RAG chunks and SQL paper metadata for the user's prompt; use web_search and arxiv_search when local evidence is insufficient or the user asks for recent/current work.
2. Summary Agent: summarize chat history and agent/tool outputs into concise working memory when context is long or results need shortening.
3. Inspiration Agent: inspect papers with curiosity, identify innovation points, research gaps, and directions the user may dive into.
4. Suggestion Agent: use the user's preferences and prior context to recommend papers and research directions the user is likely to study.
5. Tool Maker Agent: decide whether to make or adapt tools/skills when that is important; do not make tools by default.
6. Evaluation Agent: check that every factual claim has a reference or clear uncertainty label; do not allow unsupported certainty.

Operating rules:
- This is one combined chat entry. Preserve both legacy paper-focused RAG behavior and tool-capable Ace behavior.
- Prefer the current focused paper and explicitly attached papers when present.
- Use tools when the answer needs local paper lookup, parsed-paper RAG, arXiv, web search, favorites, or safe shell inspection.
- Cite sources in Chinese answers using arXiv IDs, paper titles, database paper IDs, or URLs.
- If evidence is missing, stale, contradictory, or tool configuration is unavailable, say so directly.
- Keep final answers concise, but include enough references for the user to verify.
- Do not fabricate papers, tool outputs, URLs, dates, or experimental results.
- Tool Maker Agent should only create or delete tools when the task clearly benefits and the action is safe or approved.
"""


INTENT_LABELS = {
    "research": "paper lookup, factual question, literature search, evidence gathering, local RAG, arXiv, or web search",
    "summary": "summarization or compression of long context/results",
    "inspiration": "novel ideas, innovation points, research gaps, future work, or creative directions",
    "suggestion": "recommendations, reading lists, next papers, or personalized direction finding",
    "tool_maker": "requests to create, adapt, or automate reusable tools/scripts/skills",
    "evaluation": "verification, source checking, citation review, uncertainty assessment",
}

AGENTS_BY_KEY = {agent.key: agent for agent in PAPER_ACE_AGENTS}

CLASSIFIER_SYSTEM_PROMPT = """Classify the user's research-chat intent for routing.

Return only compact JSON with this shape:
{
  "primary_intent": "research|summary|inspiration|suggestion|tool_maker|evaluation",
  "intents": ["research"],
  "agent_keys": ["research", "evaluation"],
  "confidence": 0.0,
  "rationale": "short routing reason"
}

Routing rules:
- Always include evaluation.
- Include research when the request asks for factual claims, paper lookup, source-backed answers, current/latest work, database/RAG/arXiv/web evidence, or attached/focused paper analysis.
- Include inspiration for novelty, research gaps, innovation, brainstorming, future work, or creative research angles.
- Include suggestion for recommendations, reading lists, next papers, preference-aware choices, or feedback about recommendations.
- Include summary for summarization or long context compression.
- Include tool_maker only for explicit reusable tool, script, automation, or skill requests.
- Prefer multiple agents when the request combines intents.
"""


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
    """Deterministic backup for offline/dev runs when the LLM classifier is unavailable."""
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
