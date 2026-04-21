import httpx
import asyncio
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.errors import AppError
from app.features.papers.service import PaperService
from app.features.tools.ocr_tool import PaddleOCRTool
from app.shared.http import current_user_id


router = APIRouter(prefix="/api", tags=["papers"])


class CrawlRequest(BaseModel):
    category: str = Field(default="cs.AI", min_length=2)
    maxResults: int = Field(default=20, ge=1, le=100)
    dateFilters: list[dict[str, str]] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    force: bool = False


class TranslateRequest(BaseModel):
    selection: str = Field(min_length=1)
    context: str = ""


def parse_date_filters(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []
    filters: list[dict[str, str]] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if ".." in token:
            start, end = token.split("..", 1)
            filters.append({"start": start, "end": end})
        else:
            filters.append({"start": token})
    return filters


@router.get("/config")
def public_config() -> dict:
    settings = get_settings()
    return {
        "categories": ["all", *settings.default_arxiv_category_list],
        "ocrDailyPageLimit": settings.paddleocr_daily_page_limit,
        "ocrChunkPages": settings.paddleocr_chunk_pages,
        "llmProvider": settings.llm_provider,
    }


@router.get("/papers")
def list_papers(
    category: str | None = None,
    query: str | None = None,
    limit: int = 50,
    parsed: bool | None = None,
    dates: str | None = None,
) -> dict:
    return {
        "items": PaperService().list_papers(
            category=category,
            query=query,
            limit=limit,
            parsed=parsed,
            date_filters=parse_date_filters(dates),
        )
    }


@router.get("/papers/{paper_id}")
def get_paper(paper_id: int) -> dict:
    return PaperService().get_paper(paper_id)


@router.delete("/papers/{paper_id}")
def delete_paper(paper_id: int, _: str = Depends(current_user_id)) -> dict:
    return PaperService().delete_paper(paper_id)


@router.post("/papers/crawl")
async def crawl_papers(payload: CrawlRequest, _: str = Depends(current_user_id)) -> dict:
    user_id = _
    service = PaperService()
    job = service.enqueue_crawl_job(user_id, payload.category, payload.dateFilters, payload.maxResults)
    asyncio.create_task(PaperService().run_crawl_queue())
    return job


@router.get("/papers/crawl/jobs")
def list_crawl_jobs(_: str = Depends(current_user_id)) -> dict:
    return {"items": PaperService().list_crawl_jobs(_)}


@router.get("/papers/crawl/jobs/{job_id}")
def get_crawl_job(job_id: int, _: str = Depends(current_user_id)) -> dict:
    return PaperService().get_crawl_job(job_id, _)


@router.post("/papers/{paper_id}/analyze")
async def analyze_paper(
    paper_id: int, payload: AnalyzeRequest, _: str = Depends(current_user_id)
) -> dict:
    _ = payload.force
    return await PaperService().analyze(paper_id)


@router.post("/papers/{paper_id}/translate")
async def translate_selection(
    paper_id: int, payload: TranslateRequest, _: str = Depends(current_user_id)
) -> dict:
    return await PaperService().translate_selection(paper_id, payload.selection, payload.context)


@router.post("/papers/{paper_id}/ocr")
async def submit_ocr(paper_id: int, _: str = Depends(current_user_id)) -> dict:
    paper_service = PaperService()
    paper = paper_service.get_paper(paper_id)
    pdf_url = paper["pdfUrl"]
    if "arxiv.org/pdf/" in pdf_url and not pdf_url.endswith(".pdf"):
        pdf_url = f"https://arxiv.org/pdf/{paper['arxivId']}v{paper['version']}.pdf"
    output_dir = paper_service.storage_dir_for(paper_id, paper["category"])
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "source.pdf"
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                response = await client.get(pdf_url, headers={"User-Agent": "PaperAgent/0.1"})
                if response.status_code != 200:
                    raise AppError(
                        f"Failed to download PDF for OCR. Status {response.status_code}",
                        502,
                        "pdf_download_failed",
                    )
                pdf_path.write_bytes(response.content)
        except httpx.RequestError as exc:
            raise AppError(f"Failed to download PDF for OCR: {exc}", 502, "pdf_download_failed") from exc
    return await PaddleOCRTool().submit_file(paper_id, pdf_path)


@router.post("/papers/{paper_id}/ocr/{provider_job_id}/poll")
async def poll_ocr(paper_id: int, provider_job_id: str, _: str = Depends(current_user_id)) -> dict:
    paper_service = PaperService()
    paper = paper_service.get_paper(paper_id)
    output_dir = paper_service.storage_dir_for(paper_id, paper["category"])
    return await PaddleOCRTool().poll_and_store(paper_id, provider_job_id, output_dir)


@router.get("/quota/ocr")
def ocr_quota() -> dict:
    return PaddleOCRTool().quota()
