import json
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.connection import transaction
from app.features.papers.arxiv_tool import ArxivTool
from app.features.tools.llm import ChatMessage, LLMClient


ALL_CATEGORIES = "all"
MAX_CRAWL_STEP_ATTEMPTS = 6
CRAWL_RETRY_DELAYS_SECONDS = [60, 180, 300, 600, 1200, 1800]
_crawl_queue_lock = asyncio.Lock()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def paper_to_api(row: dict) -> dict:
    settings = get_settings()
    asset_base_path = None
    storage_dir = row.get("storage_dir")
    if storage_dir:
        try:
            relative = Path(storage_dir).resolve().relative_to(settings.storage_root.resolve())
            asset_base_path = f"/storage/{relative.as_posix()}"
        except ValueError:
            asset_base_path = None
    return {
        "id": row["id"],
        "arxivId": row["arxiv_id"],
        "version": row["version"],
        "title": row["title"],
        "authors": _loads(row.get("authors"), []),
        "abstract": row.get("abstract", ""),
        "aiSummary": row.get("ai_summary", ""),
        "category": row.get("category", ""),
        "tags": _loads(row.get("tags"), []),
        "pdfUrl": row.get("pdf_url", ""),
        "absUrl": row.get("abs_url", ""),
        "publishedAt": row.get("published_at"),
        "updatedAt": row.get("updated_at"),
        "markdownPath": row.get("markdown_path"),
        "storageDir": row.get("storage_dir"),
        "assetBasePath": asset_base_path,
        "createdAt": row.get("created_at"),
        "analyzedAt": row.get("analyzed_at"),
    }


class PaperService:
    def __init__(self) -> None:
        self.arxiv = ArxivTool()
        self.llm = LLMClient()
        self.settings = get_settings()

    def list_papers(
        self,
        category: str | None = None,
        query: str | None = None,
        limit: int = 50,
        parsed: bool | None = None,
        date_filters: list[dict] | None = None,
    ) -> list[dict]:
        where: list[str] = []
        params: list[object] = []
        if category and category != ALL_CATEGORIES:
            where.append("(category = ? OR raw_metadata LIKE ?)")
            params.extend([category, f'%"{category}"%'])
        if parsed is not None:
            where.append("markdown_path IS NOT NULL" if parsed else "markdown_path IS NULL")
        if date_filters:
            date_clauses: list[str] = []
            for item in date_filters:
                start = item["start"]
                end = item.get("end") or start
                if end < start:
                    start, end = end, start
                date_clauses.append("(date(COALESCE(published_at, updated_at, created_at)) BETWEEN date(?) AND date(?))")
                params.extend([start, end])
            where.append("(" + " OR ".join(date_clauses) + ")")
        if query:
            where.append("(title LIKE ? OR abstract LIKE ? OR ai_summary LIKE ?)")
            term = f"%{query}%"
            params.extend([term, term, term])
        sql = "SELECT * FROM papers"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(updated_at, published_at, created_at) DESC LIMIT ?"
        params.append(limit)
        with transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [paper_to_api(row) for row in rows]

    def get_paper(self, paper_id: int) -> dict:
        with transaction() as connection:
            row = connection.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if not row:
            raise AppError("Paper not found", 404, "paper_not_found")
        paper = paper_to_api(row)
        paper["markdown"] = self.read_markdown(row.get("markdown_path"))
        return paper

    def search_local(self, query: str, limit: int = 12) -> list[dict]:
        terms = [token.strip() for token in query.replace("，", " ").replace(",", " ").split() if token.strip()]
        # Keep broad acronyms useful for queries like VLA/RAG/LLM.
        keywords = [term for term in terms if len(term) >= 2][:8]
        if not keywords:
            return self.list_papers(limit=limit)

        where: list[str] = []
        params: list[object] = []
        for keyword in keywords:
            where.append("(title LIKE ? OR abstract LIKE ? OR ai_summary LIKE ? OR tags LIKE ?)")
            term = f"%{keyword}%"
            params.extend([term, term, term, term])
        sql = "SELECT * FROM papers WHERE " + " OR ".join(where)
        sql += " ORDER BY COALESCE(analyzed_at, updated_at, published_at, created_at) DESC LIMIT ?"
        params.append(limit)
        with transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [paper_to_api(row) for row in rows]

    def recent_papers(self, limit: int = 20) -> list[dict]:
        with transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM papers
                ORDER BY created_at DESC, COALESCE(updated_at, published_at, created_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [paper_to_api(row) for row in rows]

    def delete_paper(self, paper_id: int) -> dict:
        with transaction() as connection:
            row = connection.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not row:
                raise AppError("Paper not found", 404, "paper_not_found")
            connection.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        return {"deleted": paper_id}

    def read_markdown(self, markdown_path: str | None) -> str:
        if not markdown_path:
            return ""
        path = Path(markdown_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    async def crawl(self, category: str, max_results: int = 20) -> dict:
        result = self._empty_crawl_result(category)
        errors: list[dict] = []
        for selected_category in self._crawl_categories(category):
            try:
                papers = await self.fetch_papers_for_date(selected_category, date.today(), max_results)
                self._merge_crawl_result(result, self._store_crawl_result(selected_category, papers))
            except httpx.HTTPError as exc:
                errors.append({"category": selected_category, "error": str(exc)})
        result["source"] = "arxiv_new"
        result["errors"] = errors
        return result

    async def crawl_by_dates(self, category: str, date_filters: list[dict], max_results: int = 20) -> dict:
        result = self._empty_crawl_result(category)
        announced_dates: list[str] = []
        fallback_dates: list[str] = []
        errors: list[dict] = []
        today = date.today()
        for item in date_filters:
            start = date.fromisoformat(item["start"])
            end = date.fromisoformat(item.get("end") or item["start"])
            if end < start:
                start, end = end, start
            current = start
            while current <= end:
                if current == today:
                    announced_dates.append(current.isoformat())
                    for selected_category in self._crawl_categories(category):
                        try:
                            papers = await self.fetch_papers_for_date(selected_category, current, max_results)
                            self._merge_crawl_result(result, self._store_crawl_result(selected_category, papers))
                        except httpx.HTTPError as exc:
                            errors.append(
                                {"date": current.isoformat(), "category": selected_category, "source": "arxiv_new", "error": str(exc)}
                            )
                else:
                    fallback_dates.append(current.isoformat())
                    for selected_category in self._crawl_categories(category):
                        try:
                            papers = await self.fetch_papers_for_date(selected_category, current, max_results)
                            self._merge_crawl_result(result, self._store_crawl_result(selected_category, papers))
                        except httpx.HTTPError as exc:
                            errors.append(
                                {"date": current.isoformat(), "category": selected_category, "source": "arxiv_api", "error": str(exc)}
                            )
                current = date.fromordinal(current.toordinal() + 1)
        result["source"] = "arxiv_new" if announced_dates and not fallback_dates else "mixed" if announced_dates else "arxiv_api"
        result["announcementDates"] = announced_dates
        result["fallbackDates"] = fallback_dates
        result["errors"] = errors
        return result

    def enqueue_crawl_job(
        self,
        user_id: str,
        category: str,
        date_filters: list[dict],
        max_results: int = 20,
    ) -> dict:
        steps = self._build_crawl_steps(category, date_filters)
        with transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO users(id, display_name, home_categories, preference_profile)
                VALUES(?, ?, '[]', '{}')
                """,
                (user_id, user_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO crawl_jobs(user_id, category, date_filters, max_results, total_steps)
                VALUES(?, ?, ?, ?, ?)
                """,
                (user_id, category, _json(date_filters), max_results, len(steps)),
            )
            job_id = cursor.lastrowid
            for index, step in enumerate(steps):
                connection.execute(
                    """
                    INSERT INTO crawl_job_steps(job_id, step_index, category, target_date, source)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (job_id, index, step["category"], step.get("target_date"), step["source"]),
                )
        return self.get_crawl_job(int(job_id), user_id)

    def list_crawl_jobs(self, user_id: str, limit: int = 10) -> list[dict]:
        with transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM crawl_jobs
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [self._crawl_job_to_api(row, include_steps=False) for row in rows]

    def get_crawl_job(self, job_id: int, user_id: str | None = None) -> dict:
        with transaction() as connection:
            params: list[object] = [job_id]
            sql = "SELECT * FROM crawl_jobs WHERE id = ?"
            if user_id:
                sql += " AND user_id = ?"
                params.append(user_id)
            row = connection.execute(sql, params).fetchone()
            if not row:
                raise AppError("Crawl job not found", 404, "crawl_job_not_found")
            steps = connection.execute(
                "SELECT * FROM crawl_job_steps WHERE job_id = ? ORDER BY step_index ASC",
                (job_id,),
            ).fetchall()
        job = self._crawl_job_to_api(row, include_steps=False)
        job["steps"] = [self._crawl_step_to_api(step) for step in steps]
        return job

    async def run_crawl_queue(self) -> None:
        async with _crawl_queue_lock:
            self._recover_interrupted_jobs()
            while True:
                job = self._next_crawl_job()
                if not job:
                    return
                await self._run_crawl_job(int(job["id"]))

    def _recover_interrupted_jobs(self) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_job_steps
                SET status = 'queued', error_message = COALESCE(error_message, 'Recovered after server restart')
                WHERE status = 'running'
                """
            )
            connection.execute(
                """
                UPDATE crawl_jobs
                SET status = 'queued', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                """
            )

    def _next_crawl_job(self) -> dict | None:
        with transaction() as connection:
            return connection.execute(
                """
                SELECT * FROM crawl_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()

    async def _run_crawl_job(self, job_id: int) -> None:
        self._mark_job_running(job_id)
        while True:
            step = self._next_crawl_step(job_id)
            if not step:
                wait_seconds = self._next_retry_wait_seconds(job_id)
                if wait_seconds is None:
                    break
                await asyncio.sleep(wait_seconds)
                continue
            await self._run_crawl_step(job_id, step)
        self._finish_crawl_job(job_id)

    def _mark_job_running(self, job_id: int) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (job_id,),
            )

    def _next_crawl_step(self, job_id: int) -> dict | None:
        with transaction() as connection:
            return connection.execute(
                """
                SELECT * FROM crawl_job_steps
                WHERE job_id = ?
                  AND status = 'queued'
                  AND (next_run_at IS NULL OR datetime(next_run_at) <= datetime('now'))
                ORDER BY step_index ASC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()

    def _next_retry_wait_seconds(self, job_id: int) -> float | None:
        with transaction() as connection:
            row = connection.execute(
                """
                SELECT next_run_at FROM crawl_job_steps
                WHERE job_id = ? AND status = 'queued' AND next_run_at IS NOT NULL
                ORDER BY datetime(next_run_at) ASC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        if not row:
            return None
        try:
            next_run_at = datetime.fromisoformat(row["next_run_at"])
        except (TypeError, ValueError):
            return 1.0
        return max(1.0, min(60.0, (next_run_at - datetime.utcnow()).total_seconds()))

    async def _run_crawl_step(self, job_id: int, step: dict) -> None:
        step_id = int(step["id"])
        with transaction() as connection:
            connection.execute(
                "UPDATE crawl_job_steps SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?",
                (step_id,),
            )
            connection.execute("UPDATE crawl_jobs SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,))
        try:
            target_date = date.fromisoformat(step["target_date"]) if step.get("target_date") else None
            if step["source"] == "arxiv_new":
                papers = await self.fetch_papers_for_date(step["category"], target_date or date.today(), self._job_max_results(job_id))
            else:
                papers = await self.fetch_papers_for_date(step["category"], target_date, self._job_max_results(job_id))
            result = self._store_crawl_result(step["category"], papers)
            self._mark_step_done(job_id, step_id, result)
        except httpx.HTTPError as exc:
            if self._is_rate_limited(exc):
                self._reschedule_step(job_id, step_id, step, str(exc))
                return
            self._mark_step_failed(job_id, step_id, str(exc))

    def _is_rate_limited(self, exc: httpx.HTTPError) -> bool:
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code == 429
        return "429" in str(exc)

    def _reschedule_step(self, job_id: int, step_id: int, step: dict, message: str) -> None:
        attempt_count = int(step.get("attempt_count") or 0) + 1
        if attempt_count > MAX_CRAWL_STEP_ATTEMPTS:
            self._mark_step_failed(job_id, step_id, f"超过最大重试次数：{message}")
            return
        delay = CRAWL_RETRY_DELAYS_SECONDS[min(attempt_count - 1, len(CRAWL_RETRY_DELAYS_SECONDS) - 1)]
        next_run_at = (datetime.utcnow() + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
        with transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_job_steps
                SET status = 'queued',
                    attempt_count = ?,
                    next_run_at = ?,
                    error_message = ?,
                    started_at = NULL
                WHERE id = ?
                """,
                (attempt_count, next_run_at, f"遇到 429，{delay} 秒后第 {attempt_count} 次重试。{message}", step_id),
            )
            connection.execute(
                """
                UPDATE crawl_jobs
                SET status = 'running',
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (f"遇到 arXiv 429，等待重试到 {next_run_at}", job_id),
            )

    def _job_max_results(self, job_id: int) -> int:
        with transaction() as connection:
            row = connection.execute("SELECT max_results FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
        return int(row["max_results"]) if row else 20

    async def fetch_papers_for_date(self, category: str, target_date: date | None, max_results: int = 20) -> list[dict]:
        if target_date is None or target_date == date.today():
            return await self.arxiv.query_announced_new(category, max_results=max_results, announced_date=target_date or date.today())
        return await self.arxiv.query(category, max_results, target_date, target_date)

    def _mark_step_done(self, job_id: int, step_id: int, result: dict) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_job_steps
                SET status = 'done', fetched_count = ?, inserted_count = ?, updated_count = ?,
                    error_message = NULL, next_run_at = NULL, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (result["fetched"], result["inserted"], result["updated"], step_id),
            )
            connection.execute(
                """
                UPDATE crawl_jobs
                SET completed_steps = completed_steps + 1,
                    fetched_count = fetched_count + ?,
                    inserted_count = inserted_count + ?,
                    updated_count = updated_count + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (result["fetched"], result["inserted"], result["updated"], job_id),
            )

    def _mark_step_failed(self, job_id: int, step_id: int, message: str) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_job_steps
                SET status = 'failed', error_message = ?, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message, step_id),
            )
            connection.execute(
                """
                UPDATE crawl_jobs
                SET completed_steps = completed_steps + 1,
                    error_message = COALESCE(error_message, ?) ,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message, job_id),
            )

    def _finish_crawl_job(self, job_id: int) -> None:
        with transaction() as connection:
            counts = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                  SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count,
                  SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_count,
                  SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
                  COUNT(*) AS total_count
                FROM crawl_job_steps
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            failed_count = int(counts["failed_count"] or 0)
            done_count = int(counts["done_count"] or 0)
            queued_count = int(counts["queued_count"] or 0)
            running_count = int(counts["running_count"] or 0)
            if queued_count or running_count:
                return
            status = "done"
            if failed_count and done_count:
                status = "partial"
            elif failed_count and not done_count:
                status = "failed"
            connection.execute(
                """
                UPDATE crawl_jobs
                SET status = ?, completed_steps = total_steps,
                    updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, job_id),
            )

    def _build_crawl_steps(self, category: str, date_filters: list[dict]) -> list[dict]:
        categories = self._crawl_categories(category)
        today = date.today()
        steps: list[dict] = []
        if not date_filters:
            return [{"category": item, "source": "arxiv_new"} for item in categories]
        for item in date_filters:
            start = date.fromisoformat(item["start"])
            end = date.fromisoformat(item.get("end") or item["start"])
            if end < start:
                start, end = end, start
            current = start
            while current <= end:
                source = "arxiv_new" if current == today else "arxiv_api"
                for selected_category in categories:
                    steps.append(
                        {
                            "category": selected_category,
                            "target_date": current.isoformat(),
                            "source": source,
                        }
                    )
                current = date.fromordinal(current.toordinal() + 1)
        return steps

    def _crawl_job_to_api(self, row: dict, include_steps: bool = False) -> dict:
        _ = include_steps
        return {
            "id": row["id"],
            "category": row["category"],
            "dateFilters": _loads(row.get("date_filters"), []),
            "maxResults": row["max_results"],
            "status": row["status"],
            "totalSteps": row["total_steps"],
            "completedSteps": row["completed_steps"],
            "fetched": row["fetched_count"],
            "inserted": row["inserted_count"],
            "updated": row["updated_count"],
            "errorMessage": row.get("error_message"),
            "createdAt": row["created_at"],
            "startedAt": row.get("started_at"),
            "updatedAt": row["updated_at"],
            "finishedAt": row.get("finished_at"),
        }

    def _crawl_step_to_api(self, row: dict) -> dict:
        return {
            "id": row["id"],
            "category": row["category"],
            "targetDate": row.get("target_date"),
            "source": row["source"],
            "status": row["status"],
            "fetched": row["fetched_count"],
            "inserted": row["inserted_count"],
            "updated": row["updated_count"],
            "errorMessage": row.get("error_message"),
            "attemptCount": row.get("attempt_count", 0),
            "nextRunAt": row.get("next_run_at"),
            "startedAt": row.get("started_at"),
            "finishedAt": row.get("finished_at"),
        }

    def _crawl_categories(self, category: str) -> list[str]:
        if category == ALL_CATEGORIES:
            return self.settings.default_arxiv_category_list
        return [category]

    def _empty_crawl_result(self, category: str) -> dict:
        return {"category": category, "fetched": 0, "inserted": 0, "updated": 0}

    def _merge_crawl_result(self, target: dict, source: dict) -> None:
        target["fetched"] += source.get("fetched", 0)
        target["inserted"] += source.get("inserted", 0)
        target["updated"] += source.get("updated", 0)

    def _store_crawl_result(self, category: str, papers: list[dict]) -> dict:
        inserted = 0
        updated = 0
        with transaction() as connection:
            for paper in papers:
                existing = connection.execute(
                    "SELECT id, version FROM papers WHERE arxiv_id = ?", (paper["arxiv_id"],)
                ).fetchone()
                if existing and int(existing["version"]) >= int(paper["version"]):
                    connection.execute(
                        """
                        UPDATE papers SET title = ?, authors = ?, abstract = ?,
                          category = ?, pdf_url = ?, abs_url = ?, published_at = ?, updated_at = ?,
                          ai_summary = COALESCE(NULLIF(?, ''), ai_summary),
                          tags = COALESCE(NULLIF(?, '[]'), tags),
                          raw_metadata = ?
                        WHERE arxiv_id = ?
                        """,
                        (
                            paper["title"],
                            _json(paper["authors"]),
                            paper["abstract"],
                            paper["category"],
                            paper["pdf_url"],
                            paper["abs_url"],
                            paper["published_at"],
                            paper["updated_at"],
                            paper.get("ai_summary", ""),
                            _json(paper.get("tags", [])),
                            _json(paper.get("raw_metadata", {})),
                            paper["arxiv_id"],
                        ),
                    )
                    updated += 1
                    continue
                if existing:
                    connection.execute(
                        """
                        UPDATE papers SET version = ?, title = ?, authors = ?, abstract = ?,
                          category = ?, pdf_url = ?, abs_url = ?, published_at = ?, updated_at = ?,
                          ai_summary = COALESCE(NULLIF(?, ''), ai_summary),
                          tags = COALESCE(NULLIF(?, '[]'), tags),
                          raw_metadata = ?, created_at = created_at
                        WHERE arxiv_id = ?
                        """,
                        (
                            paper["version"],
                            paper["title"],
                            _json(paper["authors"]),
                            paper["abstract"],
                            paper["category"],
                            paper["pdf_url"],
                            paper["abs_url"],
                            paper["published_at"],
                            paper["updated_at"],
                            paper.get("ai_summary", ""),
                            _json(paper.get("tags", [])),
                            _json(paper.get("raw_metadata", {})),
                            paper["arxiv_id"],
                        ),
                    )
                    updated += 1
                else:
                    connection.execute(
                        """
                        INSERT INTO papers(
                          arxiv_id, version, title, authors, abstract, category, pdf_url,
                          abs_url, published_at, updated_at, ai_summary, tags, raw_metadata
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            paper["arxiv_id"],
                            paper["version"],
                            paper["title"],
                            _json(paper["authors"]),
                            paper["abstract"],
                            paper["category"],
                            paper["pdf_url"],
                            paper["abs_url"],
                            paper["published_at"],
                            paper["updated_at"],
                            paper.get("ai_summary", ""),
                            _json(paper.get("tags", [])),
                            _json(paper.get("raw_metadata", {})),
                        ),
                    )
                    inserted += 1
            connection.execute(
                """
                INSERT INTO crawl_runs(category, status, fetched_count, inserted_count, updated_count)
                VALUES(?, 'done', ?, ?, ?)
                """,
                (category, len(papers), inserted, updated),
            )
        return {"category": category, "fetched": len(papers), "inserted": inserted, "updated": updated}

    def upsert_papers(self, category: str, papers: list[dict]) -> dict:
        return self._store_crawl_result(category, papers)

    def get_paper_by_arxiv_id(self, arxiv_id: str) -> dict:
        with transaction() as connection:
            row = connection.execute("SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,)).fetchone()
        if not row:
            raise AppError("Paper not found", 404, "paper_not_found")
        paper = paper_to_api(row)
        paper["markdown"] = self.read_markdown(row.get("markdown_path"))
        return paper

    def save_markdown_artifacts(self, paper_id: int, markdown_path: Path, storage_dir: Path, markdown: str) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE papers
                SET markdown_path = ?, storage_dir = ?, analyzed_at = analyzed_at
                WHERE id = ?
                """,
                (str(markdown_path), str(storage_dir), paper_id),
            )
        self.replace_chunks(paper_id, markdown)

    def storage_dir_for(self, paper_id: int, category: str) -> Path:
        safe_category = category.replace("/", "_")
        return self.settings.storage_root / date.today().isoformat() / safe_category / str(paper_id)

    async def analyze(self, paper_id: int) -> dict:
        paper = self.get_paper(paper_id)
        markdown = paper.get("markdown") or paper.get("abstract", "")
        if not markdown:
            raise AppError("No paper content or abstract is available for analysis", 400, "paper_content_missing")

        content = markdown[: self.settings.llm_max_context_chars]
        system = (
            "You are PaperAgent. Summarize research papers for a researcher. "
            "Return compact JSON with keys summary, category, tags, contributions, limitations."
        )
        prompt = (
            f"Title: {paper['title']}\n"
            f"Abstract: {paper['abstract']}\n"
            f"Content:\n{content}\n\n"
            "Use Chinese for summary and keep tags in English technical keywords."
        )
        response = await self.llm.complete(
            "paper-analysis",
            [ChatMessage("system", system), ChatMessage("user", prompt)],
        )
        parsed = self._parse_analysis(response)
        with transaction() as connection:
            connection.execute(
                """
                UPDATE papers SET ai_summary = ?, category = ?, tags = ?, analyzed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    parsed["summary"],
                    parsed.get("category") or paper["category"],
                    _json(parsed.get("tags", [])),
                    paper_id,
                ),
            )
        if markdown:
            self.replace_chunks(paper_id, markdown)
        return {"paperId": paper_id, "analysis": parsed}

    def _parse_analysis(self, response: str) -> dict:
        try:
            start = response.index("{")
            end = response.rindex("}") + 1
            parsed = json.loads(response[start:end])
        except (ValueError, json.JSONDecodeError):
            parsed = {"summary": response[:2000], "category": "", "tags": []}
        parsed.setdefault("summary", "")
        parsed.setdefault("tags", [])
        return parsed

    def replace_chunks(self, paper_id: int, content: str, chunk_size: int = 1600) -> None:
        chunks = [content[index : index + chunk_size] for index in range(0, len(content), chunk_size)]
        with transaction() as connection:
            connection.execute("DELETE FROM paper_chunks WHERE paper_id = ?", (paper_id,))
            connection.execute("DELETE FROM paper_chunks_fts WHERE paper_id = ?", (paper_id,))
            for index, chunk in enumerate(chunks):
                cursor = connection.execute(
                    "INSERT INTO paper_chunks(paper_id, chunk_index, content) VALUES(?, ?, ?)",
                    (paper_id, index, chunk),
                )
                chunk_id = cursor.lastrowid
                connection.execute(
                    "INSERT INTO paper_chunks_fts(rowid, content, paper_id, chunk_id) VALUES(?, ?, ?, ?)",
                    (chunk_id, chunk, paper_id, chunk_id),
                )

    def retrieve_context(self, paper_id: int | None, query: str, limit: int = 5) -> list[str]:
        params: list[object] = [query]
        sql = """
            SELECT c.content
            FROM paper_chunks_fts f
            JOIN paper_chunks c ON c.id = f.chunk_id
            WHERE paper_chunks_fts MATCH ?
        """
        if paper_id:
            sql += " AND f.paper_id = ?"
            params.append(paper_id)
        sql += " LIMIT ?"
        params.append(limit)
        try:
            with transaction() as connection:
                rows = connection.execute(sql, params).fetchall()
        except Exception:
            rows = []
        return [row["content"] for row in rows]

    async def translate_selection(self, paper_id: int, selection: str, context: str = "") -> dict:
        paper = self.get_paper(paper_id)
        if not selection.strip():
            raise AppError("Selection is empty", 400, "selection_empty")
        messages = [
            ChatMessage(
                "system",
                "Translate selected Markdown text into precise Chinese. Use paper context for terminology. Return only the translation.",
            ),
            ChatMessage(
                "user",
                f"Paper title: {paper['title']}\nContext: {context[:1800]}\nSelection:\n{selection[:3000]}",
            ),
        ]
        translated = await self.llm.complete("selection-translation", messages)
        return {"translation": translated}
