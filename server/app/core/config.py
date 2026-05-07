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
    llm_request_timeout_seconds: int = 120
    llm_prompt_cost_usd_per_1k: float = 0.0
    llm_completion_cost_usd_per_1k: float = 0.0

    chat_prompt_dir: Path | None = None
    chat_workflow_timeout_seconds: int = 180
    chat_agent_timeout_seconds: int = 75
    chat_tool_timeout_seconds: int = 30
    chat_llm_retry_attempts: int = 2
    chat_llm_retry_backoff_seconds: float = 1.5
    chat_parallel_agent_limit: int = 3
    chat_agent_max_tool_turns: int = 3
    chat_max_tool_calls_per_agent: int = 8
    chat_daily_user_token_budget: int = 250_000
    chat_daily_user_tool_budget: int = 200
    chat_estimated_chars_per_token: float = 3.6

    brave_api_key: str = ""

    paddleocr_token: str = ""
    paddleocr_job_url: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    paddleocr_model: str = "PaddleOCR-VL-1.5"
    paddleocr_daily_page_limit: int = 20_000
    paddleocr_chunk_pages: int = 10

    default_arxiv_categories: str = "cs.AI,cs.CL,cs.CV,cs.GR,cs.LG,stat.ML"
    daily_arxiv_enhanced_url_template: str = (
        "https://raw.githubusercontent.com/dw-dengwei/daily-arXiv-ai-enhanced/"
        "data/data/{date}_AI_enhanced_Chinese.jsonl"
    )
    crawl_interval_minutes: int = 720
    daily_paper_default_max_results: int = 12
    daily_paper_summary_chars: int = 18_000
    rag_chroma_path: Path = Path("./data/chroma")
    rag_collection_name: str = "daily_paper_chunks"
    rag_embedding_model_name: str = "Qwen/Qwen3-Embedding-0.6B"
    rag_embedding_max_length: int = 2048

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
        if self.chat_prompt_dir:
            self.chat_prompt_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_paths()
    return settings
