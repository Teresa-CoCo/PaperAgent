import asyncio
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.db.connection import transaction
from app.features.papers.service import PaperService, paper_to_api
from app.features.tools.llm import ChatMessage, LLMClient

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ToolHandler = Coroutine[Any, Any, str]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    max_results: int = 0  # 0 means no harness-level limit
    summary_fn: Callable[[dict], str] | None = None


@dataclass
class ToolContext:
    user_id: str
    paper_service: PaperService
    user_preferences: Any  # UserPreferenceService
    brave_search: Any  # BraveSearchTool
    arxiv_tool: Any  # ArxivTool
    daily_rag: Any = None  # DailyPaperRAGStore


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def search_database(
    query: str,
    limit: int = 10,
    _ctx: ToolContext | None = None,
) -> str:
    """Search papers in local SQLite database by keywords."""
    if not _ctx:
        return json.dumps({"error": "Tool context not available"})
    limit = min(limit, 30)
    results = _ctx.paper_service.search_local(query, limit=limit)
    if not results:
        return json.dumps({"results": [], "message": "No papers found matching the query."})
    compact = []
    for paper in results:
        compact.append(
            {
                "id": paper["id"],
                "arxivId": paper["arxivId"],
                "title": paper["title"],
                "category": paper.get("category", ""),
                "publishedAt": paper.get("publishedAt", ""),
                "tags": paper.get("tags", []),
                "summary": (paper.get("aiSummary") or paper.get("abstract") or "")[:800],
            }
        )
    return json.dumps({"results": compact, "total": len(compact)})


async def search_rag_database(
    paper_id: int,
    query: str,
    _ctx: ToolContext | None = None,
) -> str:
    """Search within a specific paper's parsed content (RAG chunks)."""
    if not _ctx:
        return json.dumps({"error": "Tool context not available"})
    chunks = _ctx.paper_service.retrieve_context(paper_id, query, limit=8)
    if not chunks:
        return json.dumps({"results": [], "message": "No relevant chunks found for this paper."})
    paper = _ctx.paper_service.get_paper(paper_id)
    return json.dumps(
        {
            "paper_id": paper_id,
            "paper_title": paper.get("title", ""),
            "chunks": chunks,
            "total_chunks": len(chunks),
        }
    )


async def search_daily_rag(
    query: str,
    target_date: str = "",
    category: str = "",
    max_results: int = 5,
    _ctx: ToolContext | None = None,
) -> str:
    """Search daily paper vector store (ChromaDB) by semantic similarity."""
    if not _ctx or not _ctx.daily_rag:
        return json.dumps({"error": "Daily paper RAG store not available"})
    max_results = min(max_results, 20)
    where = None
    conditions = []
    if target_date:
        conditions.append({"target_date": target_date})
    if category:
        conditions.append({"category": category})
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}
    try:
        items = _ctx.daily_rag.query(query, n_results=max_results, where=where)
    except Exception as exc:
        return json.dumps({"error": f"Daily paper RAG query failed: {exc}"})
    if not items:
        return json.dumps({"results": [], "message": "No relevant chunks found in daily papers."})
    return json.dumps({"results": items, "total": len(items)})


async def web_search(
    query: str,
    count: int = 5,
    _ctx: ToolContext | None = None,
) -> str:
    """Search the web using Brave Search API."""
    if not _ctx:
        return json.dumps({"error": "Tool context not available"})
    count = min(count, 10)
    results = await _ctx.brave_search.search(query, count=count)
    if not results:
        return json.dumps({"results": [], "message": "Web search returned no results or API key not configured."})
    return json.dumps({"results": results, "total": len(results)})


async def arxiv_search(
    query: str,
    category: str = "",
    max_results: int = 10,
    _ctx: ToolContext | None = None,
) -> str:
    """Search arXiv directly for papers."""
    if not _ctx:
        return json.dumps({"error": "Tool context not available"})
    max_results = min(max_results, 50)
    target_category = category or _ctx.paper_service.settings.default_arxiv_category_list[0]
    try:
        papers = await _ctx.arxiv_tool.search(query, target_category, max_results=max_results)
    except Exception as exc:
        return json.dumps({"error": f"arXiv search failed: {exc}"})
    compact = []
    for paper in papers:
        compact.append(
            {
                "arxivId": paper.get("arxiv_id", ""),
                "title": paper.get("title", ""),
                "authors": paper.get("authors", [])[:4],
                "abstract": (paper.get("abstract") or "")[:600],
                "category": paper.get("category", ""),
                "pdfUrl": paper.get("pdf_url", ""),
                "absUrl": paper.get("abs_url", ""),
            }
        )
    return json.dumps({"results": compact, "total": len(compact)})


async def list_favorite_folders(
    _ctx: ToolContext | None = None,
) -> str:
    """List all favorite folders."""
    if not _ctx:
        return json.dumps({"error": "Tool context not available"})
    folders = _ctx.user_preferences.favorite_folders(_ctx.user_id)
    return json.dumps({"folders": folders})


async def add_to_favorites(
    paper_ids: list[int],
    folder_name: str = "Default",
    _ctx: ToolContext | None = None,
) -> str:
    """Add papers to a favorites folder. Creates the folder if it doesn't exist."""
    if not _ctx:
        return json.dumps({"error": "Tool context not available"})
    result = {"added": 0, "errors": [], "folder_name": folder_name}

    folder = _ctx.user_preferences.create_folder(_ctx.user_id, folder_name)
    folder_id = folder["id"]
    result["folder_id"] = folder_id

    for paper_id in paper_ids:
        try:
            _ctx.user_preferences.favorite_paper(_ctx.user_id, paper_id, folder_id)
            result["added"] += 1
        except Exception as exc:
            result["errors"].append(f"paper_id={paper_id}: {exc}")
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, ToolDef] = {}


def register_tool(name: str, description: str, parameters: dict) -> Callable:
    """Decorator to register a tool. The handler must be an async function."""
    def decorator(handler: ToolHandler) -> ToolHandler:
        _TOOL_REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )
        return handler
    return decorator


def all_tools() -> list[ToolDef]:
    tools = list(_TOOL_REGISTRY.values())
    tools.extend([
        ToolDef(
            name="search_database",
            description=(
                "Search papers in the local SQLite database by keywords/topic. "
                "Use this to find papers already crawled from arXiv. "
                "Returns title, arxivId, category, summary, and tags."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords, e.g. 'computer vision transformer' or 'LLM reasoning'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results (default 10, max 30)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            handler=search_database,
        ),
        ToolDef(
            name="search_rag_database",
            description=(
                "Search within a specific paper's parsed markdown content (RAG chunks). "
                "Use when the user asks detailed questions about a specific paper's content, "
                "methodology, results, or implementation details."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paper_id": {
                        "type": "integer",
                        "description": "Paper ID from the database (e.g. from search_database results)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Specific question about the paper content",
                    },
                },
                "required": ["paper_id", "query"],
            },
            handler=search_rag_database,
        ),
        ToolDef(
            name="search_daily_rag",
            description=(
                "Search the daily paper vector store (ChromaDB) using semantic similarity. "
                "Use when the user asks about recent papers, daily digest content, "
                "or wants to find semantically similar content across daily papers. "
                "Supports filtering by target_date and category."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Semantic search query",
                    },
                    "target_date": {
                        "type": "string",
                        "description": "Filter by target date (YYYY-MM-DD format). Optional.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by arXiv category (e.g. cs.AI, cs.CL). Optional.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results (default 5, max 20)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            handler=search_daily_rag,
        ),
        ToolDef(
            name="web_search",
            description=(
                "Search the web using Brave Search for real-time information, "
                "recent news, latest research trends, or topics not in the local database."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Web search query",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (default 5, max 10)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            handler=web_search,
        ),
        ToolDef(
            name="arxiv_search",
            description=(
                "Search arXiv directly for papers by category. "
                "Use this to find papers not yet in the local database, "
                "or to search specific arXiv categories in real-time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords (used as context category search)",
                    },
                    "category": {
                        "type": "string",
                        "description": "arXiv category (e.g. cs.CV, cs.AI, cs.CL, cs.LG). Omits to use default.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results (default 10, max 50)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            handler=arxiv_search,
        ),
        ToolDef(
            name="list_favorite_folders",
            description="List all favorite folders and their IDs for the current user.",
            parameters={
                "type": "object",
                "properties": {},
            },
            handler=list_favorite_folders,
        ),
        ToolDef(
            name="add_to_favorites",
            description=(
                "Add papers to a favorites folder. Creates the folder if it doesn't exist. "
                "Use this to save papers the user wants to keep."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paper_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Array of paper IDs to favorite",
                    },
                    "folder_name": {
                        "type": "string",
                        "description": "Folder name (created if doesn't exist). Default 'Default'.",
                        "default": "Default",
                    },
                },
                "required": ["paper_ids"],
            },
            handler=add_to_favorites,
        ),
    ])
    if get_settings().chat_shell_tool_enabled:
        tools.append(ToolDef(
            name="shell_execute",
            description=(
                "Execute a local development shell command. "
                "Use this to run terminal commands: file operations (mkdir, cp, mv, curl, wget), "
                "git, python scripts, package checks, or any system command. "
                "Safe commands run automatically; destructive ones (rm, sudo) are blocked."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute. For safety, destructive commands require user approval.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120)",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
            handler=shell_execute,
        ))
    return tools


# ---------------------------------------------------------------------------
# Shell execution with safety classification
# ---------------------------------------------------------------------------

SAFE_COMMAND_PREFIXES = (
    "ls", "pwd", "echo ", "cat ", "head ", "tail ", "wc ", "sort ", "grep ",
    "find ", "which ", "whoami", "date", "uname", "df ", "du ", "free",
    "ps ", "env", "printenv",
    "node", "npm", "tsc",
    "git status", "git log", "git diff", "git branch", "git remote",
    "git config", "pip list", "pip show",
    "mkdir", "mkdir ",
    "cp ", "mv ",
    "touch ", "chmod ",
    "file ", "stat ", "readlink",
    "ping ", "dig ", "nslookup",
)


def is_dangerous_command(command: str) -> str | None:
    """Return a reason string if the command is dangerous, None if it's safe to auto-run."""
    stripped = command.strip()
    if not stripped:
        return "Empty command"
    for prefix in SAFE_COMMAND_PREFIXES:
        if stripped.startswith(prefix):
            return None
    first_word = stripped.split(maxsplit=1)[0] if stripped else ""
    return f"Command '{first_word}' is not in the safe list and requires user approval"


async def shell_execute(
    command: str,
    timeout: int = 30,
    _ctx: ToolContext | None = None,
) -> str:
    """Execute a shell command and return its output.

    Non-dangerous commands are executed directly. Dangerous commands
    are blocked and return an error. The harness enforces an outer timeout.
    """
    danger = is_dangerous_command(command)
    if danger is not None:
        return json.dumps({
            "error": f"Command blocked: {danger}",
            "hint": "This command is not in the safe list. "
                    "The tool maker agent can help wrap approved commands into reusable tools.",
        })
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    error = stderr.decode("utf-8", errors="replace")
    result = {
        "return_code": proc.returncode,
        "stdout": output[-2000:],
        "stderr": error[-1000:],
    }
    if proc.returncode != 0:
        result["error"] = f"Command exited with return code {proc.returncode}"
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Tool calling helpers
# ---------------------------------------------------------------------------

def tool_definitions() -> list[dict]:
    """Return tool definitions in OpenAI/DeepSeek-compatible format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in all_tools()
    ]


def tool_map() -> dict[str, ToolDef]:
    return {tool.name: tool for tool in all_tools()}


def _validate_arguments(kwargs: dict, schema: dict) -> str | None:
    """Return error message if kwargs don't match schema, None if valid."""
    required = schema.get("required", [])
    for field in required:
        if field not in kwargs:
            return f"Missing required parameter: {field}"
    properties = schema.get("properties", {})
    for key, value in kwargs.items():
        if key not in properties:
            continue  # Allow extra params for flexibility
        expected_type = properties[key].get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"Parameter '{key}' must be a string"
        if expected_type == "integer" and not isinstance(value, int):
            return f"Parameter '{key}' must be an integer"
        if expected_type == "array" and not isinstance(value, list):
            return f"Parameter '{key}' must be an array"
    return None


async def execute_tool(
    name: str,
    arguments: str,
    ctx: ToolContext,
) -> str:
    """Execute a tool by name with JSON arguments and tool context."""
    tools = tool_map()
    tool = tools.get(name)
    if not tool:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        kwargs = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid arguments JSON: {exc}"})
    error = _validate_arguments(kwargs, tool.parameters)
    if error:
        return json.dumps({"error": error})
    return await tool.handler(**kwargs, _ctx=ctx)
