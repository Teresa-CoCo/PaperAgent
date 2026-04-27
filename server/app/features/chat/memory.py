import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.db.connection import transaction


MEMORY_AGENT_KEYS = {"research", "suggestion", "inspiration"}


@dataclass
class AgentMemory:
    agent_key: str
    data: dict[str, Any]

    def brief(self) -> str:
        if self.agent_key == "research":
            topics = self.data.get("topics", {})
            domains = self.data.get("domains", {})
            return (
                f"Frequent research topics: {_top_items(topics)}.\n"
                f"Frequent source domains: {_top_items(domains)}."
            )
        if self.agent_key == "suggestion":
            return (
                f"Positive recommendation signals: {_top_items(self.data.get('positive', {}))}.\n"
                f"Negative recommendation signals: {_top_items(self.data.get('negative', {}))}."
            )
        if self.agent_key == "inspiration":
            return (
                f"Preferred innovation styles: {_top_items(self.data.get('styles', {}))}.\n"
                f"Creative patterns: {_top_items(self.data.get('patterns', {}))}."
            )
        return "No dedicated memory."


class AgentMemoryStore:
    def get(self, user_id: str, agent_key: str) -> AgentMemory:
        with transaction() as connection:
            row = connection.execute(
                "SELECT memory_json FROM agent_memories WHERE user_id = ? AND agent_key = ?",
                (user_id, agent_key),
            ).fetchone()
        if not row:
            return AgentMemory(agent_key, self._empty(agent_key))
        try:
            data = json.loads(row["memory_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        return AgentMemory(agent_key, data or self._empty(agent_key))

    def get_many(self, user_id: str, agent_keys: list[str]) -> dict[str, AgentMemory]:
        return {key: self.get(user_id, key) for key in agent_keys if key in MEMORY_AGENT_KEYS}

    def update_from_turn(self, user_id: str, message: str, final_answer: str, agent_outputs: dict[str, str]) -> None:
        if agent_outputs.get("research"):
            memory = self.get(user_id, "research")
            _merge_counts(memory.data.setdefault("topics", {}), _keywords(message))
            _merge_counts(memory.data.setdefault("domains", {}), _domains(final_answer))
            self.save(user_id, memory)

        if agent_outputs.get("suggestion"):
            memory = self.get(user_id, "suggestion")
            bucket = "negative" if _contains_negative_feedback(message) else "positive" if _contains_positive_feedback(message) else "neutral"
            _merge_counts(memory.data.setdefault(bucket, {}), _keywords(message))
            self.save(user_id, memory)

        if agent_outputs.get("inspiration"):
            memory = self.get(user_id, "inspiration")
            _merge_counts(memory.data.setdefault("styles", {}), _innovation_styles(message))
            _merge_counts(memory.data.setdefault("patterns", {}), _keywords(message))
            self.save(user_id, memory)

    def save(self, user_id: str, memory: AgentMemory) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO agent_memories(user_id, agent_key, memory_json, updated_at)
                VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, agent_key)
                DO UPDATE SET memory_json = excluded.memory_json, updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, memory.agent_key, json.dumps(_trim_memory(memory.data), ensure_ascii=False)),
            )

    def _empty(self, agent_key: str) -> dict[str, Any]:
        if agent_key == "research":
            return {"topics": {}, "domains": {}}
        if agent_key == "suggestion":
            return {"positive": {}, "negative": {}, "neutral": {}}
        if agent_key == "inspiration":
            return {"styles": {}, "patterns": {}}
        return {}


def _keywords(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
    stop = {"the", "and", "for", "with", "paper", "papers", "what", "that", "this", "please", "recommend", "suggest"}
    return [token for token in tokens if token not in stop][:20]


def _domains(text: str) -> list[str]:
    return [match.group(1).lower() for match in re.finditer(r"https?://([^/\s)]+)", text)]


def _innovation_styles(text: str) -> list[str]:
    lowered = text.lower()
    styles: list[str] = []
    for token in ("theoretical", "engineering", "benchmark", "system", "dataset", "architecture", "multimodal", "agent", "rag"):
        if token in lowered:
            styles.append(token)
    for token in ("理论", "工程", "系统", "数据集", "架构", "多模态", "智能体"):
        if token in text:
            styles.append(token)
    return styles or ["open-ended"]


def _contains_positive_feedback(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("like", "liked", "useful", "good", "great", "喜欢", "有用", "不错"))


def _contains_negative_feedback(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("dislike", "bad", "not useful", "irrelevant", "不喜欢", "没用", "无关"))


def _merge_counts(target: dict[str, int], items: list[str]) -> None:
    counts = Counter(target)
    counts.update(items)
    target.clear()
    target.update(dict(counts.most_common(24)))


def _trim_memory(data: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            trimmed[key] = dict(Counter(value).most_common(24))
        else:
            trimmed[key] = value
    return trimmed


def _top_items(items: dict[str, int]) -> str:
    if not items:
        return "none yet"
    return ", ".join(f"{key} ({value})" for key, value in Counter(items).most_common(6))
