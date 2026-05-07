Classify the user's research-chat intent for routing.

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
