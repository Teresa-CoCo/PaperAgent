Extract durable memory from one user-assistant turn.

Return strict JSON with this shape:
{
  "session_summary": "string",
  "working_summary": "string",
  "open_questions": ["string"],
  "salient_facts": ["string"],
  "user_profile": {
    "interests": ["string"],
    "goals": ["string"],
    "likes": ["string"],
    "dislikes": ["string"],
    "constraints": ["string"]
  },
  "episode_summary": "string",
  "episode_topics": ["string"]
}

Rules:
- Use only high-confidence information present in the conversation.
- Keep summaries compact and factual.
- If something is uncertain, leave it out instead of guessing.
- Keep profile fields sparse; only include durable user preferences or goals.
- `salient_facts` should contain reusable facts or stable references, not generic fluff.
- `session_summary` is the durable, evolving summary of the whole session so far.
- `working_summary` is a compact compression of the conversation up to and including this turn, suitable as immediate context for the next turn's agents. It should fold in the previous working summary plus the latest exchange.
