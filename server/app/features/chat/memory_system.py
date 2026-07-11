import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from langgraph.store.base import BaseStore

from app.features.chat.prompts import get_prompt_store
from app.features.chat.runtime import estimate_tokens
from app.features.tools.llm import ChatMessage, LLMClient


SESSION_NAMESPACE = ("memory", "session")
PROFILE_NAMESPACE = ("memory", "profile")
EPISODE_NAMESPACE = ("memory", "episode")


@dataclass
class SessionMemory:
    summary: str = ""
    open_questions: list[str] = field(default_factory=list)
    salient_facts: list[str] = field(default_factory=list)
    updated_at: str = ""
    working_summary: str = ""

    def brief(self) -> str:
        parts: list[str] = []
        if self.summary:
            parts.append(f"Session summary: {self.summary}")
        if self.open_questions:
            parts.append("Open questions: " + "; ".join(self.open_questions[:4]))
        if self.salient_facts:
            parts.append("Salient facts: " + "; ".join(self.salient_facts[:6]))
        return "\n".join(parts) or "No session summary yet."


@dataclass
class ProfileMemory:
    interests: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    updated_at: str = ""

    def brief(self) -> str:
        parts = [
            ("Interests", self.interests),
            ("Goals", self.goals),
            ("Likes", self.likes),
            ("Dislikes", self.dislikes),
            ("Constraints", self.constraints),
        ]
        lines = [f"{label}: {', '.join(values[:6])}" for label, values in parts if values]
        return "\n".join(lines) or "No user profile memory yet."


@dataclass
class EpisodeMemory:
    key: str
    summary: str
    topics: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    created_at: str = ""

    def brief(self) -> str:
        topic_text = ", ".join(self.topics[:5]) if self.topics else "none"
        refs = ", ".join(self.source_refs[:4]) if self.source_refs else "none"
        return f"{self.summary} | topics: {topic_text} | refs: {refs}"


@dataclass
class MemoryBundle:
    session: SessionMemory = field(default_factory=SessionMemory)
    profile: ProfileMemory = field(default_factory=ProfileMemory)
    episodes: list[EpisodeMemory] = field(default_factory=list)
    working_summary: str = ""

    def prompt_block(self) -> str:
        parts = [
            "Session memory:\n" + self.session.brief(),
            "Profile memory:\n" + self.profile.brief(),
        ]
        if self.working_summary:
            parts.append("Working summary:\n" + self.working_summary)
        if self.episodes:
            episode_lines = [f"{index}. {episode.brief()}" for index, episode in enumerate(self.episodes[:4], start=1)]
            parts.append("Relevant past episodes:\n" + "\n".join(episode_lines))
        return "\n\n".join(parts)


class ConversationMemoryManager:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.prompts = get_prompt_store()

    def load_bundle(self, store: BaseStore | None, user_id: str, session_id: str, query: str) -> MemoryBundle:
        if not store:
            return MemoryBundle()
        session_item = store.get((*SESSION_NAMESPACE, user_id), session_id)
        profile_item = store.get((*PROFILE_NAMESPACE, user_id), "profile")
        episode_items = store.search((*EPISODE_NAMESPACE, user_id), query=query, limit=4)
        bundle = MemoryBundle(
            session=self._session_from_item(session_item.value if session_item else {}),
            profile=self._profile_from_item(profile_item.value if profile_item else {}),
            episodes=[self._episode_from_item(item.key, item.value) for item in episode_items],
        )
        bundle.working_summary = bundle.session.working_summary
        return bundle

    def should_summarize_history(self, session_history: list[dict], existing_summary: str) -> bool:
        if len(session_history) >= 10:
            return True
        history_chars = sum(len((row.get("content") or "")) for row in session_history)
        if history_chars >= 12000:
            return True
        return not existing_summary and history_chars >= 7000

    async def summarize_history(
        self,
        session_history: list[dict],
        previous_summary: str,
    ) -> str:
        if not session_history:
            return previous_summary
        history_lines = []
        for row in session_history[-10:]:
            role = row.get("role", "user")
            content = (row.get("content") or "").strip()
            if not content:
                continue
            history_lines.append(f"{role}: {content[:1800]}")
        prompt = self.prompts.render("memory_history_summary")
        if not self.llm.settings.llm_api_key:
            condensed = "\n".join(history_lines)
            text = (previous_summary + "\n" + condensed).strip()
            return text[-2400:]
        response = await self.llm.complete(
            "memory-history-summary",
            [
                ChatMessage("system", prompt),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "previous_summary": previous_summary,
                            "recent_history": history_lines,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            use_cache=False,
        )
        return (response.content or previous_summary).strip()[:2400]

    async def persist_turn(
        self,
        *,
        store: BaseStore | None,
        user_id: str,
        session_id: str,
        message: str,
        final_answer: str,
        session_history: list[dict],
        existing_bundle: MemoryBundle,
        source_refs: list[str],
    ) -> None:
        if not store:
            return
        payload = await self._extract_turn_memory(
            message=message,
            final_answer=final_answer,
            session_history=session_history,
            existing_bundle=existing_bundle,
            source_refs=source_refs,
        )
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        session_value = {
            "summary": payload.get("session_summary") or existing_bundle.session.summary,
            "working_summary": payload.get("working_summary") or "",
            "open_questions": _unique_list(payload.get("open_questions") or existing_bundle.session.open_questions),
            "salient_facts": _unique_list(payload.get("salient_facts") or existing_bundle.session.salient_facts),
            "updated_at": now,
        }
        profile_value = _merge_profile(existing_bundle.profile, payload.get("user_profile") or {}, now)
        episode_summary = str(payload.get("episode_summary") or "").strip()
        if not episode_summary:
            episode_summary = f"{message[:120]} -> {final_answer[:180]}"
        episode_topics = _unique_list(payload.get("episode_topics") or _keyword_guess(message))
        episode_key = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        episode_value = {
            "summary": episode_summary[:600],
            "topics": episode_topics[:10],
            "source_refs": source_refs[:12],
            "created_at": now,
        }
        store.put((*SESSION_NAMESPACE, user_id), session_id, session_value)
        store.put((*PROFILE_NAMESPACE, user_id), "profile", profile_value)
        store.put((*EPISODE_NAMESPACE, user_id), episode_key, episode_value)

    def clear_user_memory(self, store: Any, user_id: str) -> dict:
        deleted = 0
        deleted += store.delete_namespace_prefix((*SESSION_NAMESPACE, user_id))
        deleted += store.delete_namespace_prefix((*PROFILE_NAMESPACE, user_id))
        deleted += store.delete_namespace_prefix((*EPISODE_NAMESPACE, user_id))
        return {"deletedMemoryItems": deleted}

    def clear_session_memory(self, store: Any, user_id: str, session_id: str) -> bool:
        before = store.get((*SESSION_NAMESPACE, user_id), session_id)
        store.put((*SESSION_NAMESPACE, user_id), session_id, None)
        return before is not None

    async def _extract_turn_memory(
        self,
        *,
        message: str,
        final_answer: str,
        session_history: list[dict],
        existing_bundle: MemoryBundle,
        source_refs: list[str],
    ) -> dict[str, Any]:
        if not self.llm.settings.llm_api_key:
            return {
                "session_summary": f"{existing_bundle.session.summary}\nUser: {message[:160]}\nAssistant: {final_answer[:240]}".strip()[:1200],
                "working_summary": f"{existing_bundle.session.summary}\nUser: {message[:160]}\nAssistant: {final_answer[:240]}".strip()[:1200],
                "open_questions": existing_bundle.session.open_questions[:4],
                "salient_facts": _unique_list(_keyword_guess(message) + _keyword_guess(final_answer))[:8],
                "user_profile": {
                    "interests": _keyword_guess(message),
                },
                "episode_summary": f"User asked about {message[:120]} and received {final_answer[:200]}",
                "episode_topics": _keyword_guess(message),
            }
        prompt = self.prompts.render("memory_turn_extractor")
        compact_history = [
            {
                "role": row.get("role"),
                "content": (row.get("content") or "")[:1200],
            }
            for row in session_history[-6:]
            if row.get("content")
        ]
        response = await self.llm.complete(
            "memory-turn-extractor",
            [
                ChatMessage("system", prompt),
                ChatMessage(
                    "user",
                    json.dumps(
                        {
                            "message": message,
                            "assistant_answer": final_answer,
                            "previous_session_summary": existing_bundle.session.summary,
                            "existing_profile": {
                                "interests": existing_bundle.profile.interests,
                                "goals": existing_bundle.profile.goals,
                                "likes": existing_bundle.profile.likes,
                                "dislikes": existing_bundle.profile.dislikes,
                                "constraints": existing_bundle.profile.constraints,
                            },
                            "recent_history": compact_history,
                            "source_refs": source_refs[:12],
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
            use_cache=False,
        )
        return _parse_json_block(response.content)

    def _session_from_item(self, value: dict[str, Any]) -> SessionMemory:
        return SessionMemory(
            summary=str(value.get("summary") or ""),
            open_questions=_unique_list(value.get("open_questions") or []),
            salient_facts=_unique_list(value.get("salient_facts") or []),
            updated_at=str(value.get("updated_at") or ""),
            working_summary=str(value.get("working_summary") or ""),
        )

    def _profile_from_item(self, value: dict[str, Any]) -> ProfileMemory:
        return ProfileMemory(
            interests=_unique_list(value.get("interests") or []),
            goals=_unique_list(value.get("goals") or []),
            likes=_unique_list(value.get("likes") or []),
            dislikes=_unique_list(value.get("dislikes") or []),
            constraints=_unique_list(value.get("constraints") or []),
            updated_at=str(value.get("updated_at") or ""),
        )

    def _episode_from_item(self, key: str, value: dict[str, Any]) -> EpisodeMemory:
        return EpisodeMemory(
            key=key,
            summary=str(value.get("summary") or ""),
            topics=_unique_list(value.get("topics") or []),
            source_refs=_unique_list(value.get("source_refs") or []),
            created_at=str(value.get("created_at") or ""),
        )


def _merge_profile(existing: ProfileMemory, payload: dict[str, Any], updated_at: str) -> dict[str, Any]:
    return {
        "interests": _unique_list(existing.interests + list(payload.get("interests") or []))[:12],
        "goals": _unique_list(existing.goals + list(payload.get("goals") or []))[:10],
        "likes": _unique_list(existing.likes + list(payload.get("likes") or []))[:10],
        "dislikes": _unique_list(existing.dislikes + list(payload.get("dislikes") or []))[:10],
        "constraints": _unique_list(existing.constraints + list(payload.get("constraints") or []))[:10],
        "updated_at": updated_at,
    }


def _unique_list(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _keyword_guess(text: str) -> list[str]:
    tokens = [token.strip(".,:;!?()[]{}").lower() for token in text.split()]
    return _unique_list([token for token in tokens if len(token) >= 4])[:10]


def _parse_json_block(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if "```" in cleaned:
        cleaned = cleaned.replace("```json", "```").strip()
        parts = [part.strip() for part in cleaned.split("```") if part.strip()]
        if parts:
            cleaned = parts[0]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}
