import json
from datetime import date
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.db.connection import transaction


class PaddleOCRTool:
    def __init__(self) -> None:
        self.settings = get_settings()

    def quota(self) -> dict:
        today = date.today().isoformat()
        with transaction() as connection:
            row = connection.execute(
                "SELECT * FROM quota_usage WHERE usage_date = ?", (today,)
            ).fetchone()
        return {
            "date": today,
            "pagesUsed": row["pages_used"] if row else 0,
            "dailyLimit": row["daily_limit"] if row else self.settings.paddleocr_daily_page_limit,
        }

    def add_usage(self, pages: int) -> None:
        today = date.today().isoformat()
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO quota_usage(usage_date, pages_used, daily_limit)
                VALUES(?, ?, ?)
                ON CONFLICT(usage_date) DO UPDATE SET
                  pages_used = pages_used + excluded.pages_used,
                  daily_limit = excluded.daily_limit
                """,
                (today, pages, self.settings.paddleocr_daily_page_limit),
            )

    async def submit_url(self, paper_id: int, file_url: str) -> dict:
        if not self.settings.paddleocr_token:
            raise AppError("PaddleOCR token is not configured", 400, "ocr_token_missing")
        quota = self.quota()
        if quota["pagesUsed"] >= quota["dailyLimit"]:
            raise AppError("PaddleOCR daily quota has been exhausted", 429, "ocr_quota_exhausted")

        headers = {
            "Authorization": f"bearer {self.settings.paddleocr_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "fileUrl": file_url,
            "model": self.settings.paddleocr_model,
            "optionalPayload": {
                "useDocOrientationClassify": False,
                "useDocUnwarping": False,
                "useChartRecognition": False,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(self.settings.paddleocr_job_url, json=payload, headers=headers)
                if response.status_code != 200:
                    raise AppError(
                        f"PaddleOCR rejected the file URL. Status {response.status_code}: {response.text[:800]}",
                        502,
                        "ocr_submit_failed",
                    )
                job_id = response.json()["data"]["jobId"]
        except httpx.RequestError as exc:
            raise AppError(f"PaddleOCR request failed: {exc}", 502, "ocr_request_failed") from exc

        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO ocr_jobs(paper_id, provider_job_id, status)
                VALUES(?, ?, 'pending')
                """,
                (paper_id, job_id),
            )
        return {"jobId": job_id, "status": "pending"}

    async def submit_file(self, paper_id: int, file_path: Path) -> dict:
        if not self.settings.paddleocr_token:
            raise AppError("PaddleOCR token is not configured", 400, "ocr_token_missing")
        if not file_path.exists():
            raise AppError("PDF file does not exist for OCR upload", 400, "ocr_file_missing")
        quota = self.quota()
        if quota["pagesUsed"] >= quota["dailyLimit"]:
            raise AppError("PaddleOCR daily quota has been exhausted", 429, "ocr_quota_exhausted")

        headers = {"Authorization": f"bearer {self.settings.paddleocr_token}"}
        data = {
            "model": self.settings.paddleocr_model,
            "optionalPayload": json.dumps(
                {
                    "useDocOrientationClassify": False,
                    "useDocUnwarping": False,
                    "useChartRecognition": False,
                }
            ),
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                with file_path.open("rb") as file_handle:
                    response = await client.post(
                        self.settings.paddleocr_job_url,
                        headers=headers,
                        data=data,
                        files={"file": (file_path.name, file_handle, "application/pdf")},
                    )
                if response.status_code != 200:
                    raise AppError(
                        f"PaddleOCR rejected the PDF upload. Status {response.status_code}: {response.text[:800]}",
                        502,
                        "ocr_submit_failed",
                    )
                job_id = response.json()["data"]["jobId"]
        except httpx.RequestError as exc:
            raise AppError(f"PaddleOCR request failed: {exc}", 502, "ocr_request_failed") from exc

        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO ocr_jobs(paper_id, provider_job_id, status)
                VALUES(?, ?, 'pending')
                """,
                (paper_id, job_id),
            )
        return {"jobId": job_id, "status": "pending"}

    async def poll_and_store(self, paper_id: int, provider_job_id: str, output_dir: Path) -> dict:
        if not self.settings.paddleocr_token:
            raise AppError("PaddleOCR token is not configured", 400, "ocr_token_missing")

        headers = {"Authorization": f"bearer {self.settings.paddleocr_token}"}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(f"{self.settings.paddleocr_job_url}/{provider_job_id}", headers=headers)
                if response.status_code != 200:
                    raise AppError(
                        f"PaddleOCR poll failed. Status {response.status_code}: {response.text[:800]}",
                        502,
                        "ocr_poll_failed",
                    )
                data = response.json()["data"]
        except httpx.RequestError as exc:
            raise AppError(f"PaddleOCR request failed: {exc}", 502, "ocr_request_failed") from exc

        state = data["state"]
        progress = data.get("extractProgress", {})
        pages_total = int(progress.get("totalPages", 0) or 0)
        pages_extracted = int(progress.get("extractedPages", 0) or 0)
        result_url = data.get("resultUrl", {}).get("jsonUrl")
        error_message = data.get("errorMsg")

        with transaction() as connection:
            connection.execute(
                """
                UPDATE ocr_jobs
                SET status = ?, pages_total = ?, pages_extracted = ?, result_url = ?,
                    error_message = ?, updated_at = CURRENT_TIMESTAMP
                WHERE provider_job_id = ?
                """,
                (state, pages_total, pages_extracted, result_url, error_message, provider_job_id),
            )

        if state == "done" and result_url:
            markdown_path = await self._download_result(client=None, result_url=result_url, output_dir=output_dir)
            self.add_usage(pages_extracted)
            with transaction() as connection:
                connection.execute(
                    "UPDATE papers SET markdown_path = ?, storage_dir = ? WHERE id = ?",
                    (str(markdown_path), str(output_dir), paper_id),
                )
            return {"status": state, "markdownPath": str(markdown_path), "pagesExtracted": pages_extracted}

        if state == "failed":
            raise AppError(error_message or "PaddleOCR job failed", 502, "ocr_failed")

        return {"status": state, "pagesTotal": pages_total, "pagesExtracted": pages_extracted}

    async def _download_result(self, client: httpx.AsyncClient | None, result_url: str, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        close_client = client is None
        active_client = client or httpx.AsyncClient(timeout=60)
        try:
            response = await active_client.get(result_url)
            response.raise_for_status()
            lines = [line.strip() for line in response.text.splitlines() if line.strip()]
            combined: list[str] = []
            page_num = 0
            for line in lines:
                result = json.loads(line)["result"]
                for parsed in result.get("layoutParsingResults", []):
                    markdown = parsed.get("markdown", {})
                    combined.append(markdown.get("text", ""))
                    await self._save_images(active_client, markdown.get("images", {}), output_dir)
                    await self._save_images(active_client, parsed.get("outputImages", {}), output_dir, page_num)
                    page_num += 1
            markdown_path = output_dir / "paper.md"
            markdown_path.write_text("\n\n".join(combined), encoding="utf-8")
            return markdown_path
        finally:
            if close_client:
                await active_client.aclose()

    async def _save_images(
        self,
        client: httpx.AsyncClient,
        images: dict[str, str],
        output_dir: Path,
        page_num: int | None = None,
    ) -> None:
        for name, url in images.items():
            suffix = f"_{page_num}.jpg" if page_num is not None and "." not in name else ""
            target = output_dir / f"{name}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            response = await client.get(url)
            if response.status_code == 200:
                target.write_bytes(response.content)
