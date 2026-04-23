import asyncio
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.features.daily_papers.service import DailyPaperService
from app.shared.http import current_user_id


router = APIRouter(prefix="/api/daily-papers", tags=["daily-papers"])


class DailyPaperRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    categories: list[str] = Field(default_factory=list)
    target_date: Annotated[
        str | None,
        Field(default=None, validation_alias=AliasChoices("targetDate", "target_date")),
    ]
    max_results: Annotated[
        int,
        Field(default=12, validation_alias=AliasChoices("maxResults", "max_results"), ge=1, le=2000),
    ]


def _parse_categories(value: str | None) -> Sequence[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.get("")
def list_daily_papers(targetDate: str | None = None, categories: str | None = None) -> dict:
    service = DailyPaperService()
    return {"items": service.list_daily_papers(targetDate, _parse_categories(categories))}


@router.get("/runs")
def list_runs(user_id: str = Depends(current_user_id)) -> dict:
    return {"items": DailyPaperService().list_runs(user_id)}


@router.get("/runs/{run_id}")
def get_run(run_id: int, user_id: str = Depends(current_user_id)) -> dict:
    return DailyPaperService().get_run(run_id, user_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, user_id: str = Depends(current_user_id)) -> dict:
    return DailyPaperService().cancel_run(run_id, user_id)


@router.post("/runs")
async def create_run(
    payload: DailyPaperRunRequest = Body(default_factory=DailyPaperRunRequest),
    user_id: str = Depends(current_user_id),
) -> dict:
    service = DailyPaperService()
    run = service.create_run(
        user_id,
        payload.categories,
        payload.target_date or service.default_target_date(),
        payload.max_results,
    )
    asyncio.create_task(service.run_queue())
    return run
