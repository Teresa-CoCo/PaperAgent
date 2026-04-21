from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends

from app.features.users.service import UserPreferenceService, ensure_user
from app.shared.http import current_user_id


router = APIRouter(prefix="/api/users", tags=["users"])


class PreferenceRequest(BaseModel):
    text: str = Field(min_length=1)


class FolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class FavoriteRequest(BaseModel):
    paperId: int
    folderId: int | None = None


@router.post("/preferences")
def update_preferences(payload: PreferenceRequest, user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().update_from_text(user_id, payload.text)


@router.get("/recommendations")
async def recommendations(user_id: str = Depends(current_user_id), limit: int = 10) -> dict:
    ensure_user(user_id)
    return {"items": await UserPreferenceService().ai_recommendations(user_id, limit)}


@router.get("/settings")
def user_settings(user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().settings(user_id)


@router.put("/settings/preferences")
def update_preference_text(payload: PreferenceRequest, user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().update_preference_text(user_id, payload.text)


@router.delete("/settings/chat-memory")
def clear_chat_memory(user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().clear_chat_memory(user_id)


@router.delete("/settings/unfavorited-papers")
def delete_unfavorited_papers(user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().delete_unfavorited_papers(user_id)


@router.get("/favorites/folders")
def favorite_folders(user_id: str = Depends(current_user_id)) -> dict:
    return {"items": UserPreferenceService().favorite_folders(user_id)}


@router.post("/favorites/folders")
def create_favorite_folder(payload: FolderRequest, user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().create_folder(user_id, payload.name)


@router.post("/favorites")
def favorite_paper(payload: FavoriteRequest, user_id: str = Depends(current_user_id)) -> dict:
    return UserPreferenceService().favorite_paper(user_id, payload.paperId, payload.folderId)


@router.get("/favorites")
def favorite_papers(
    user_id: str = Depends(current_user_id), folderId: int | None = None, limit: int = 50
) -> dict:
    return {"items": UserPreferenceService().favorite_papers(user_id, folderId, limit)}
