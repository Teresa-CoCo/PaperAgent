from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.logging import logger
from app.features.papers.service import PaperService
from app.features.users.service import ensure_user


def create_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler()

    async def crawl_defaults() -> None:
        service = PaperService()
        try:
            ensure_user("scheduler")
            service.enqueue_crawl_job("scheduler", "all", [], 20)
            await service.run_crawl_queue()
        except Exception as exc:
            logger.error("scheduled crawl enqueue failed error=%s", exc)

    scheduler.add_job(
        crawl_defaults,
        "interval",
        minutes=settings.crawl_interval_minutes,
        id="crawl-default-arxiv-categories",
        replace_existing=True,
    )
    return scheduler
