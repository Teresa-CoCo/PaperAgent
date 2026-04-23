import asyncio
import json
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.connection import transaction
from app.features.papers.arxiv_tool import ArxivTool
from app.features.papers.service import PaperService
from app.features.tools.llm import ChatMessage, LLMClient
from app.features.users.service import ensure_user


_daily_queue_lock = asyncio.Lock()
DAILY_PAPER_PIPELINE_CONCURRENCY = 2


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _exc_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__


class DailyPaperRAGStore:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._collection = None
        self._embedder = None

    def _embedding_function(self):
        if self._embedder is not None:
            return self._embedder
        model_name = self.settings.rag_embedding_model_name
        try:
            import torch
            import torch.nn.functional as F
            from transformers import AutoModel, AutoTokenizer

            device = "cuda" if torch.cuda.is_available() else "cpu"
            tokenizer = AutoTokenizer.from_pretrained(model_name, padding_side="left", trust_remote_code=True)
            model = AutoModel.from_pretrained(model_name, trust_remote_code=True)
            model.to(device)
            model.eval()

            def last_token_pool(last_hidden_states, attention_mask):
                left_padding = attention_mask[:, -1].sum() == attention_mask.shape[0]
                if bool(left_padding):
                    return last_hidden_states[:, -1]
                sequence_lengths = attention_mask.sum(dim=1) - 1
                batch_size = last_hidden_states.shape[0]
                return last_hidden_states[
                    torch.arange(batch_size, device=last_hidden_states.device),
                    sequence_lengths,
                ]

            class TransformersEmbedding:
                def __call__(self, input: list[str]) -> list[list[float]]:
                    batch_dict = tokenizer(
                        input,
                        padding=True,
                        truncation=True,
                        max_length=8192,
                        return_tensors="pt",
                    )
                    batch_dict = {key: value.to(device) for key, value in batch_dict.items()}
                    with torch.no_grad():
                        outputs = model(**batch_dict)
                    embeddings = last_token_pool(outputs.last_hidden_state, batch_dict["attention_mask"])
                    embeddings = F.normalize(embeddings, p=2, dim=1)
                    return embeddings.cpu().tolist()

            self._embedder = TransformersEmbedding()
            return self._embedder
        except Exception as exc:
            raise AppError(
                f"Embedding model unavailable. Install transformers>=4.51.0 and torch for {model_name}: {exc}",
                500,
                "embedding_unavailable",
            ) from exc

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        try:
            import chromadb
        except Exception as exc:
            raise AppError(
                f"ChromaDB is not installed: {exc}",
                500,
                "chromadb_unavailable",
            ) from exc
        client = chromadb.PersistentClient(path=str(self.settings.rag_chroma_path))
        self._collection = client.get_or_create_collection(
            name=self.settings.rag_collection_name,
            embedding_function=self._embedding_function(),
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    def upsert_daily_paper(
        self,
        paper_id: int,
        target_date: str,
        category: str,
        title: str,
        markdown: str,
    ) -> int:
        collection = self._get_collection()
        chunks = self._chunk_markdown(markdown)
        if not chunks:
            return 0
        base = f"{target_date}:{category}:{paper_id}"
        ids = [f"{base}:{index}" for index in range(len(chunks))]
        metadatas = [
            {
                "paper_id": paper_id,
                "target_date": target_date,
                "category": category,
                "title": title,
                "chunk_index": index,
            }
            for index in range(len(chunks))
        ]
        collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        return len(chunks)

    def _chunk_markdown(self, markdown: str, chunk_size: int = 1400, overlap: int = 160) -> list[str]:
        text = markdown.strip()
        if not text:
            return []
        chunks: list[str] = []
        step = max(200, chunk_size - overlap)
        for start in range(0, len(text), step):
            chunk = text[start : start + chunk_size].strip()
            if chunk:
                chunks.append(chunk)
        return chunks


class DailyPaperService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.arxiv = ArxivTool()
        self.papers = PaperService()
        self.llm = LLMClient()
        self.rag = DailyPaperRAGStore()

    def default_target_date(self) -> str:
        return (date.today() - timedelta(days=1)).isoformat()

    def create_run(self, user_id: str, categories: Sequence[str], target_date: str, max_results: int | None = None) -> dict:
        ensure_user(user_id)
        normalized_categories = self._normalize_categories(categories)
        resolved_max_results = max_results or self.settings.daily_paper_default_max_results
        with transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO daily_paper_runs(user_id, target_date, categories, max_results)
                VALUES(?, ?, ?, ?)
                """,
                (user_id, target_date, _json(normalized_categories), resolved_max_results),
            )
            run_id = int(cursor.lastrowid)
        return self.get_run(run_id, user_id)

    def list_runs(self, user_id: str, limit: int = 12) -> list[dict]:
        ensure_user(user_id)
        with transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM daily_paper_runs
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._run_to_api(row) for row in rows]

    def get_run(self, run_id: int, user_id: str | None = None) -> dict:
        params: list[object] = [run_id]
        sql = "SELECT * FROM daily_paper_runs WHERE id = ?"
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        with transaction() as connection:
            row = connection.execute(sql, params).fetchone()
        if not row:
            raise AppError("Daily paper run not found", 404, "daily_paper_run_not_found")
        return self._run_to_api(row)

    def cancel_run(self, run_id: int, user_id: str) -> dict:
        ensure_user(user_id)
        with transaction() as connection:
            row = connection.execute(
                "SELECT * FROM daily_paper_runs WHERE id = ? AND user_id = ?",
                (run_id, user_id),
            ).fetchone()
            if not row:
                raise AppError("Daily paper run not found", 404, "daily_paper_run_not_found")
            if row["status"] in ("done", "failed", "partial", "cancelled"):
                return self._run_to_api(row)
            connection.execute(
                """
                UPDATE daily_paper_runs
                SET status = 'cancelled',
                    error_message = COALESCE(NULLIF(error_message, ''), 'Stopped by user'),
                    updated_at = CURRENT_TIMESTAMP,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (run_id,),
            )
            cancelled = connection.execute("SELECT * FROM daily_paper_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_to_api(cancelled)

    def list_daily_papers(self, target_date: str | None = None, categories: Sequence[str] | None = None) -> list[dict]:
        filters: list[str] = []
        params: list[object] = []
        if target_date:
            filters.append("d.target_date = ?")
            params.append(target_date)
        normalized_categories = [item.strip() for item in (categories or []) if item and item.strip()]
        if normalized_categories:
            placeholders = ",".join("?" for _ in normalized_categories)
            filters.append(f"d.category IN ({placeholders})")
            params.extend(normalized_categories)
        sql = """
            SELECT
              d.*,
              p.arxiv_id,
              p.version,
              p.title,
              p.authors,
              p.abstract,
              p.ai_summary,
              p.pdf_url,
              p.abs_url,
              p.published_at,
              p.updated_at,
              p.tags
            FROM daily_papers d
            JOIN papers p ON p.id = d.paper_id
        """
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY d.target_date DESC, d.category ASC, p.updated_at DESC, p.published_at DESC, d.id DESC"
        with transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._daily_paper_to_api(row) for row in rows]

    async def run_queue(self) -> None:
        async with _daily_queue_lock:
            self._recover_interrupted_runs()
            while True:
                run = self._next_run()
                if not run:
                    return
                await self._run_single(int(run["id"]))

    def _recover_interrupted_runs(self) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE daily_paper_runs
                SET status = 'queued', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            )

    def _next_run(self) -> dict | None:
        with transaction() as connection:
            return connection.execute(
                """
                SELECT * FROM daily_paper_runs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

    async def _run_single(self, run_id: int) -> None:
        with transaction() as connection:
            row = connection.execute("SELECT * FROM daily_paper_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                raise AppError("Daily paper run not found", 404, "daily_paper_run_not_found")
            if row["status"] == "cancelled":
                return
            connection.execute(
                """
                UPDATE daily_paper_runs
                SET status = 'running',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP,
                    error_message = NULL
                WHERE id = ?
                """,
                (run_id,),
            )
        target_date = row["target_date"]
        categories = self._normalize_categories(_loads(row.get("categories"), []))
        max_results = int(row["max_results"])
        total_papers = 0
        completed = 0
        inserted = 0
        updated = 0
        errors: list[str] = []
        progress_lock = asyncio.Lock()

        for category in categories:
            if self._is_cancelled(run_id):
                self._finish_cancelled_run(run_id, total_papers, completed, inserted, updated, errors)
                return
            try:
                crawled = await self.papers.fetch_papers_for_date(
                    category,
                    date.fromisoformat(target_date),
                    max_results,
                )
                total_papers += len(crawled)
                store_result = self.papers.upsert_papers(category, crawled)
                inserted += int(store_result["inserted"])
                updated += int(store_result["updated"])
                self._update_run_progress(run_id, total_papers=total_papers, inserted=inserted, updated=updated)
                semaphore = asyncio.Semaphore(DAILY_PAPER_PIPELINE_CONCURRENCY)

                async def process_item(item: dict) -> None:
                    nonlocal completed
                    if self._is_cancelled(run_id):
                        return
                    async with semaphore:
                        try:
                            await self._process_daily_paper(run_id, target_date, category, item)
                        except Exception as exc:
                            async with progress_lock:
                                errors.append(f"{category}/{item['arxiv_id']}: {_exc_text(exc)}")
                        finally:
                            async with progress_lock:
                                completed += 1
                                self._update_run_progress(
                                    run_id,
                                    total_papers=total_papers,
                                    completed=completed,
                                    inserted=inserted,
                                    updated=updated,
                                    error_message=errors[0] if errors else None,
                                )

                await asyncio.gather(*(process_item(item) for item in crawled))
                if self._is_cancelled(run_id):
                    self._finish_cancelled_run(run_id, total_papers, completed, inserted, updated, errors)
                    return
            except Exception as exc:
                errors.append(f"{category}: {_exc_text(exc)}")

        status = "done"
        if errors and completed:
            status = "partial"
        elif errors and not completed:
            status = "failed"
        with transaction() as connection:
            connection.execute(
                """
                UPDATE daily_paper_runs
                SET status = ?, total_papers = ?, completed_papers = ?, inserted_count = ?, updated_count = ?,
                    error_message = ?, updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    status,
                    total_papers,
                    completed,
                    inserted,
                    updated,
                    "\n".join(errors[:8]) if errors else None,
                    run_id,
                ),
            )

    async def _process_daily_paper(self, run_id: int, target_date: str, category: str, arxiv_paper: dict) -> None:
        paper = self.papers.get_paper_by_arxiv_id(arxiv_paper["arxiv_id"])
        existing = self._existing_daily_paper(target_date, category, paper["id"])
        storage_dir = self._storage_dir(target_date, category, paper["arxivId"])
        storage_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = storage_dir / "source.pdf"
        markdown_path = storage_dir / "paper.md"
        await self._download_pdf(paper["pdfUrl"], pdf_path)
        conversion_error = None
        if existing and existing.get("markdown_path") and Path(existing["markdown_path"]).exists():
            markdown = Path(existing["markdown_path"]).read_text(encoding="utf-8")
        elif markdown_path.exists():
            markdown = markdown_path.read_text(encoding="utf-8")
        else:
            markdown, conversion_error = await asyncio.to_thread(self._convert_pdf_to_markdown, pdf_path, paper)
            markdown_path.write_text(markdown, encoding="utf-8")
            self.papers.save_markdown_artifacts(paper["id"], markdown_path, storage_dir, markdown)
        rag_document_count = 0
        rag_error = None
        if existing and int(existing.get("rag_document_count") or 0) > 0:
            rag_document_count = int(existing["rag_document_count"])
        else:
            try:
                rag_document_count = await asyncio.to_thread(
                    self.rag.upsert_daily_paper,
                    paper["id"],
                    target_date,
                    category,
                    paper["title"],
                    markdown,
                )
            except Exception as exc:
                rag_error = _exc_text(exc)
        summaries = await self._summarize(paper, markdown)
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO daily_papers(
                  run_id, paper_id, target_date, category, short_summary, long_summary,
                  markdown_path, rag_collection, rag_document_count, status, error_message, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(target_date, paper_id, category) DO UPDATE SET
                  run_id = excluded.run_id,
                  short_summary = excluded.short_summary,
                  long_summary = excluded.long_summary,
                  markdown_path = excluded.markdown_path,
                  rag_collection = excluded.rag_collection,
                  rag_document_count = excluded.rag_document_count,
                  status = 'ready',
                  error_message = NULL,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    run_id,
                    paper["id"],
                    target_date,
                    category,
                    summaries["short_summary"],
                    summaries["long_summary"],
                    str(markdown_path),
                    self.settings.rag_collection_name,
                    rag_document_count,
                ),
            )
            if rag_error or conversion_error:
                connection.execute(
                    """
                    UPDATE daily_papers
                    SET error_message = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE target_date = ? AND paper_id = ? AND category = ?
                    """,
                    (" | ".join([item for item in [conversion_error, rag_error] if item]), target_date, paper["id"], category),
                )

    def _is_cancelled(self, run_id: int) -> bool:
        with transaction() as connection:
            row = connection.execute("SELECT status FROM daily_paper_runs WHERE id = ?", (run_id,)).fetchone()
        return bool(row and row["status"] == "cancelled")

    def _finish_cancelled_run(
        self,
        run_id: int,
        total_papers: int,
        completed: int,
        inserted: int,
        updated: int,
        errors: list[str],
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE daily_paper_runs
                SET status = 'cancelled',
                    total_papers = ?,
                    completed_papers = ?,
                    inserted_count = ?,
                    updated_count = ?,
                    error_message = COALESCE(NULLIF(error_message, ''), ?, 'Stopped by user'),
                    updated_at = CURRENT_TIMESTAMP,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (total_papers, completed, inserted, updated, "\n".join(errors[:8]) if errors else "Stopped by user", run_id),
            )

    def _update_run_progress(
        self,
        run_id: int,
        total_papers: int,
        completed: int = 0,
        inserted: int = 0,
        updated: int = 0,
        error_message: str | None = None,
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE daily_paper_runs
                SET total_papers = ?, completed_papers = ?, inserted_count = ?, updated_count = ?,
                    error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (total_papers, completed, inserted, updated, error_message, run_id),
            )

    def _normalize_categories(self, categories: Sequence[str]) -> list[str]:
        normalized = [item.strip() for item in categories if item and item.strip()]
        return normalized or list(self.settings.default_arxiv_category_list)

    def _storage_dir(self, target_date: str, category: str, arxiv_id: str) -> Path:
        safe_category = category.replace("/", "_")
        safe_arxiv_id = arxiv_id.replace("/", "_")
        return self.settings.storage_root / "daily-paper" / target_date / safe_category / safe_arxiv_id

    async def _download_pdf(self, pdf_url: str, pdf_path: Path) -> None:
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(pdf_url, headers={"User-Agent": "PaperAgent/0.1"})
            response.raise_for_status()
            pdf_path.write_bytes(response.content)

    def _convert_pdf_to_markdown(self, pdf_path: Path, paper: dict) -> tuple[str, str | None]:
        try:
            from markitdown import MarkItDown

            result = MarkItDown().convert(str(pdf_path))
            content = getattr(result, "text_content", "") or str(result)
            if content.strip():
                return content, None
        except Exception as exc:
            conversion_error = _exc_text(exc)
        else:
            conversion_error = "MarkItDown returned empty content"
        return (
            f"# {paper['title']}\n\n"
            f"- arXiv: {paper['arxivId']}\n"
            f"- Category: {paper['category']}\n"
            f"- Authors: {', '.join(paper['authors'])}\n\n"
            "## Abstract\n\n"
            f"{paper['abstract']}\n"
        ), conversion_error

    async def _summarize(self, paper: dict, markdown: str) -> dict:
        content = markdown[: self.settings.daily_paper_summary_chars]
        response = await self.llm.complete(
            "daily-paper-summary",
            [
                ChatMessage(
                    "system",
                    (
                        "You summarize research papers for a Daily Paper page. "
                        "Return strict JSON with keys short_summary and long_summary. "
                        "Both must be Chinese markdown. short_summary should be under 120 Chinese characters. "
                        "long_summary should cover problem, method, findings, and limitations."
                    ),
                ),
                ChatMessage(
                    "user",
                    f"Title: {paper['title']}\nAbstract: {paper['abstract']}\nContent:\n{content}",
                ),
            ],
        )
        try:
            start = response.index("{")
            end = response.rindex("}") + 1
            parsed = json.loads(response[start:end])
        except (ValueError, json.JSONDecodeError):
            parsed = {
                "short_summary": (paper["abstract"] or paper["title"])[:120],
                "long_summary": response[:4000] or paper["abstract"],
            }
        parsed.setdefault("short_summary", (paper["abstract"] or paper["title"])[:120])
        parsed.setdefault("long_summary", paper["abstract"])
        return parsed

    def _run_to_api(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "targetDate": row["target_date"],
            "categories": _loads(row.get("categories"), []),
            "maxResults": row["max_results"],
            "status": row["status"],
            "totalPapers": row["total_papers"],
            "completedPapers": row["completed_papers"],
            "inserted": row["inserted_count"],
            "updated": row["updated_count"],
            "errorMessage": row.get("error_message"),
            "createdAt": row["created_at"],
            "startedAt": row.get("started_at"),
            "updatedAt": row["updated_at"],
            "finishedAt": row.get("finished_at"),
        }

    def _existing_daily_paper(self, target_date: str, category: str, paper_id: int) -> dict | None:
        with transaction() as connection:
            return connection.execute(
                """
                SELECT * FROM daily_papers
                WHERE target_date = ? AND category = ? AND paper_id = ?
                """,
                (target_date, category, paper_id),
            ).fetchone()

    def _daily_paper_to_api(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "runId": row.get("run_id"),
            "paperId": row["paper_id"],
            "targetDate": row["target_date"],
            "category": row["category"],
            "title": row["title"],
            "authors": _loads(row.get("authors"), []),
            "abstract": row.get("abstract", ""),
            "shortSummary": row.get("short_summary", ""),
            "longSummary": row.get("long_summary", ""),
            "status": row.get("status", "queued"),
            "errorMessage": row.get("error_message"),
            "markdownPath": row.get("markdown_path"),
            "ragCollection": row.get("rag_collection"),
            "ragDocumentCount": row.get("rag_document_count", 0),
            "arxivId": row["arxiv_id"],
            "version": row["version"],
            "pdfUrl": row.get("pdf_url", ""),
            "absUrl": row.get("abs_url", ""),
            "publishedAt": row.get("published_at"),
            "updatedAt": row.get("updated_at"),
            "tags": _loads(row.get("tags"), []),
        }
