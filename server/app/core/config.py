from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    database_path: Path = Path("./data/paper_agent.sqlite3")
    storage_root: Path = Path("./data/storage")

    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_interface: str = "chat_completions"
    llm_max_context_chars: int = 14_000

    brave_api_key: str = ""

    paddleocr_token: str = ""
    paddleocr_job_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    paddleocr_model: str = "PaddleOCR-VL-1.5"
    paddleocr_daily_page_limit: int = 20_000
    paddleocr_chunk_pages: int = 10

    default_arxiv_categories: str = "cs.AI,cs.CL,cs.CV,cs.GR,cs.LG,stat.ML"
    crawl_interval_minutes: int = 720
    daily_paper_default_max_results: int = 12
    daily_paper_summary_chars: int = 18_000
    rag_chroma_path: Path = Path("./data/chroma")
    rag_collection_name: str = "daily_paper_chunks"
    rag_embedding_model_name: str = "Qwen/Qwen3-Embedding-0.6B"

    @staticmethod
    def csv_list(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return self.csv_list(self.cors_origins)

    @property
    def default_arxiv_category_list(self) -> list[str]:
        return self.csv_list(self.default_arxiv_categories)

    def ensure_paths(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.rag_chroma_path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_paths()
    return settings
