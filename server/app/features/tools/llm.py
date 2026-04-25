import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from collections.abc import AsyncIterator

from app.core.config import get_settings
from app.db.connection import transaction


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class ToolCall:
    id: str
    function: dict  # {"name": str, "arguments": str}


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] | None = None


class LLMClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _cache_key(self, task: str, payload: dict) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(f"{task}:{serialized}".encode("utf-8")).hexdigest()

    def _read_cache(self, cache_key: str) -> str | None:
        with transaction() as connection:
            row = connection.execute(
                "SELECT response_text FROM llm_cache WHERE cache_key = ?", (cache_key,)
            ).fetchone()
        return row["response_text"] if row else None

    def _write_cache(self, cache_key: str, response_text: str) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO llm_cache(cache_key, response_text, created_at)
                VALUES(?, ?, CURRENT_TIMESTAMP)
                """,
                (cache_key, response_text),
            )

    async def complete(
        self,
        task: str,
        messages: list[ChatMessage],
        use_cache: bool = True,
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [message.__dict__ for message in messages],
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools

        cache_key = self._cache_key(task, payload)
        if use_cache and not tools:
            cached = self._read_cache(cache_key)
            if cached:
                return LLMResponse(content=cached)

        if not self.settings.llm_api_key:
            joined = "\n".join(message.content for message in messages[-2:])
            response_text = (
                "LLM API key is not configured. Local fallback summary:\n"
                + joined[:1200]
            )
            if use_cache:
                self._write_cache(cache_key, response_text)
            return LLMResponse(content=response_text)

        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            if self.settings.llm_interface == "responses":
                response = await client.post(
                    f"{self.settings.llm_base_url.rstrip('/')}/v1/responses",
                    headers=headers,
                    json={
                        "model": self.settings.llm_model,
                        "input": [message.__dict__ for message in messages],
                    },
                )
                response.raise_for_status()
                data = response.json()
                return LLMResponse(content=data.get("output_text") or json.dumps(data, ensure_ascii=False))

            response = await client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]
            msg = choice.get("message", {})

        response_text = msg.get("content") or ""
        raw_tool_calls = msg.get("tool_calls")

        tool_calls = None
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                tool_calls.append(
                    ToolCall(
                        id=tc["id"],
                        function={"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
                    )
                )

        if use_cache and not tool_calls:
            self._write_cache(cache_key, response_text)

        return LLMResponse(content=response_text, tool_calls=tool_calls)

    async def stream(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        if not self.settings.llm_api_key:
            fallback = await self.complete("stream-fallback", messages, use_cache=False)
            for index in range(0, len(fallback), 48):
                yield fallback[index : index + 48]
            return

        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model,
            "messages": [message.__dict__ for message in messages],
            "temperature": 0.2,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ").strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                        delta = payload["choices"][0].get("delta", {}).get("content")
                    except (KeyError, json.JSONDecodeError, IndexError):
                        delta = None
                    if delta:
                        yield delta
