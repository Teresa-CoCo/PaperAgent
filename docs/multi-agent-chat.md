# Paper Ace Paper Multi-Agent Chat

Paper Ace Paper is the single chat entry for the research workspace. It replaces the old UI split between Paper Chat and Ace Chat while preserving both behaviors:

- Paper-focused RAG over the active paper and selected text.
- Tool-capable research actions over local SQL, parsed-paper chunks, arXiv, web search, favorites, and approved shell commands.

## Agent Contract

The backend exposes six fixed agents from `server/app/features/chat/agents.py`:

| Agent | Responsibility |
| --- | --- |
| Research Agent | Searches SQL paper metadata, parsed RAG chunks, arXiv, and web sources. |
| Summary Agent | Compresses long chat history and tool/agent outputs. |
| Inspiration Agent | Finds innovation points, research gaps, and dive-deeper directions. |
| Suggestion Agent | Recommends papers and directions from user preferences. |
| Tool Maker Agent | Decides whether a reusable tool or skill is worth creating. Use sparingly. |
| Evaluation Agent | Checks references, uncertainty, and unsupported claims before final answers. |

The frontend fetches this catalog from `GET /api/chat/agents` and displays all six agents in the chat panel. Streaming responses emit `agent_start` and `agent_result` events for the agents selected for the current turn.

## Context Caching

DeepSeek context caching works best when request prefixes remain stable. Keep `PAPER_ACE_AGENT_CHARTER` stable and put volatile data in later messages:

1. Stable system message: six-agent charter and operating rules.
2. Runtime system message: date, user id, active agent keys.
3. Session history.
4. Current user prompt, active paper, attachments, selection, and RAG prefetch.

Avoid adding request-specific text to the stable charter unless the agent contract itself changes.

## Compatibility

The API still accepts legacy `paper` and `ace` mode/scope values, but normalizes newly created sessions and requests to `paper_ace`. Legacy sessions remain visible in history and can still be opened.
