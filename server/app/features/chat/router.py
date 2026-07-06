from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.features.chat.service import ChatService
from app.shared.http import current_user_id


router = APIRouter(prefix="/api/chat", tags=["chat"])
missions_router = APIRouter(prefix="/api/missions", tags=["missions"])


class SessionRequest(BaseModel):
    scope: str = Field(default="paper_ace", pattern="^(paper|ace|paper_ace)$")
    paperId: int | None = None
    title: str = ""


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    paperId: int | None = None
    selection: str | None = Field(default=None, max_length=20_000)
    attachmentPaperIds: list[int] = Field(default_factory=list, max_length=20)
    mode: str = Field(default="paper_ace", pattern="^(paper|ace|paper_ace)$")


@router.post("/sessions")
def create_session(payload: SessionRequest, user_id: str = Depends(current_user_id)) -> dict:
    return ChatService().create_session(user_id, payload.scope, payload.paperId, payload.title)


@router.get("/sessions")
def list_sessions(user_id: str = Depends(current_user_id)) -> dict:
    return {"items": ChatService().list_sessions(user_id)}


@router.get("/agents")
def list_agents(_: str = Depends(current_user_id)) -> dict:
    return {"items": ChatService().agents()}


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, user_id: str = Depends(current_user_id)) -> dict:
    return {"items": ChatService().messages(session_id, user_id)}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, user_id: str = Depends(current_user_id)) -> dict:
    return ChatService().delete_session(user_id, session_id)


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str, payload: MessageRequest, user_id: str = Depends(current_user_id)
) -> dict:
    return await ChatService().reply(
        user_id=user_id,
        session_id=session_id,
        message=payload.message,
        paper_id=payload.paperId,
        selection=payload.selection,
        attachment_paper_ids=payload.attachmentPaperIds,
        mode=payload.mode,
    )


@router.post("/sessions/{session_id}/submit")
def submit_message(
    session_id: str, payload: MessageRequest, user_id: str = Depends(current_user_id)
) -> dict:
    return ChatService().submit_mission(
        user_id=user_id,
        session_id=session_id,
        message=payload.message,
        paper_id=payload.paperId,
        selection=payload.selection,
        attachment_paper_ids=payload.attachmentPaperIds,
        mode=payload.mode,
    )


@router.post("/sessions/{session_id}/stream")
async def stream_message(
    session_id: str, payload: MessageRequest, user_id: str = Depends(current_user_id)
) -> StreamingResponse:
    async def event_stream():
        async for chunk in ChatService().stream_reply(
            user_id=user_id,
            session_id=session_id,
            message=payload.message,
            paper_id=payload.paperId,
            selection=payload.selection,
            attachment_paper_ids=payload.attachmentPaperIds,
            mode=payload.mode,
        ):
            yield chunk

    return StreamingResponse(event_stream(), media_type="application/x-ndjson; charset=utf-8")


class SessionUpdateRequest(BaseModel):
    paperId: int | None = None


@router.patch("/sessions/{session_id}")
def update_session(session_id: str, payload: SessionUpdateRequest, user_id: str = Depends(current_user_id)) -> dict:
    from app.db.connection import transaction
    from app.core.errors import AppError
    with transaction() as conn:
        row = conn.execute("SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
        if not row:
            raise AppError("Chat session not found", 404, "chat_session_not_found")
        conn.execute("UPDATE chat_sessions SET paper_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (payload.paperId, session_id))
    return {"id": session_id, "paperId": payload.paperId}


@router.get("/usage")
def get_usage(user_id: str = Depends(current_user_id)) -> dict:
    from app.features.chat.workflow_store import ChatWorkflowStore
    from app.features.chat.runtime import today_key, get_chat_runtime_settings
    store = ChatWorkflowStore()
    runtime = get_chat_runtime_settings()
    usage = store.daily_usage(user_id, today_key())
    return {
        "dailyTokenBudget": runtime.daily_user_token_budget,
        "dailyToolBudget": runtime.daily_user_tool_budget,
        "today": {
            "promptTokens": usage["prompt_tokens"],
            "completionTokens": usage["completion_tokens"],
            "totalTokens": usage["total_tokens"],
            "toolCalls": usage["tool_calls"],
            "estimatedCostUsd": usage["estimated_cost_usd"],
        },
    }


@missions_router.get("/{mission_id}")
def get_mission(mission_id: int, user_id: str = Depends(current_user_id)) -> dict:
    return ChatService().get_mission(mission_id, user_id)
