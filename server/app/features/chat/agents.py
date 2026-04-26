from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    purpose: str
    when_to_use: str


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


def select_agents(message: str, has_long_history: bool = False) -> list[AgentSpec]:
    lowered = message.lower()
    selected: list[AgentSpec] = [PAPER_ACE_AGENTS[0]]
    if has_long_history or any(token in lowered for token in ("summary", "summarize", "总结", "概括", "简短")):
        selected.append(PAPER_ACE_AGENTS[1])
    if any(
        token in lowered
        for token in (
            "idea",
            "inspire",
            "inspiration",
            "innovation",
            "novel",
            "future",
            "gap",
            "method inspire",
            "research on",
            "创新",
            "启发",
            "灵感",
            "方向",
            "不足",
        )
    ):
        selected.append(PAPER_ACE_AGENTS[2])
    if any(token in lowered for token in ("recommend", "suggest", "next", "reading", "推荐", "建议", "下一篇", "偏好")):
        selected.append(PAPER_ACE_AGENTS[3])
    if any(token in lowered for token in ("tool", "skill", "script", "自动化", "工具", "技能", "脚本")):
        selected.append(PAPER_ACE_AGENTS[4])
    selected.append(PAPER_ACE_AGENTS[5])

    seen: set[str] = set()
    unique: list[AgentSpec] = []
    for agent in selected:
        if agent.key in seen:
            continue
        seen.add(agent.key)
        unique.append(agent)
    return unique
