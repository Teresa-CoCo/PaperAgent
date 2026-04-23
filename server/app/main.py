from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import request_context_middleware, security_headers_middleware
from app.db.connection import init_db
from app.features.chat.router import router as chat_router
from app.features.daily_papers.router import router as daily_papers_router
from app.features.papers.router import router as papers_router
from app.features.users.router import router as users_router
from app.features.daily_papers.service import DailyPaperService
from app.features.papers.service import PaperService
from app.scheduler import create_scheduler


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    scheduler = create_scheduler()
    scheduler.start()
    asyncio.create_task(PaperService().run_crawl_queue())
    asyncio.create_task(DailyPaperService().run_queue())
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PaperAgent", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.middleware("http")(request_context_middleware)
    app.middleware("http")(security_headers_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(papers_router)
    app.include_router(daily_papers_router)
    app.include_router(chat_router)
    app.include_router(users_router)
    app.mount("/storage", StaticFiles(directory=settings.storage_root), name="storage")
    return app


app = create_app()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    init_db()
    return {"status": "ready"}
