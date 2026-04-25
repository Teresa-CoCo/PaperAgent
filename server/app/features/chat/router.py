from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.features.chat.service import ChatService
from app.shared.http import current_user_id


router = APIRouter(prefix="/api/chat", tags=["chat"])


class SessionRequest(BaseModel):
    scope: str = Field(pattern="^(paper|ace)$")
    paperId: int | None = None
    title: str = ""


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)
    paperId: int | None = None
    selection: str | None = None
    mode: str = Field(default="paper", pattern="^(paper|ace)$")


@router.post("/sessions")
def create_session(payload: SessionRequest, user_id: str = Depends(current_user_id)) -> dict:
    return ChatService().create_session(user_id, payload.scope, payload.paperId, payload.title)


@router.get("/sessions")
def list_sessions(user_id: str = Depends(current_user_id)) -> dict:
    return {"items": ChatService().list_sessions(user_id)}


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, _: str = Depends(current_user_id)) -> dict:
    return {"items": ChatService().messages(session_id)}


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
            mode=payload.mode,
        ):
            yield chunk

    return StreamingResponse(event_stream(), media_type="application/x-ndjson; charset=utf-8")


class ApproveRequest(BaseModel):
    approved: bool = True


@router.post("/sessions/{session_id}/tools/{tool_call_id}/approve")
def approve_tool_call(
    session_id: str,
    tool_call_id: str,
    payload: ApproveRequest,
    _: str = Depends(current_user_id),
) -> dict:
    ok = ChatService.approve_tool_call(tool_call_id, payload.approved)
    if not ok:
        return {"status": "not_found", "message": "No pending approval for this tool call"}
    return {"status": "approved" if payload.approved else "denied"}
