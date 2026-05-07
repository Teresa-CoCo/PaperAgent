from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha1
from pathlib import Path

from app.core.config import get_settings


@dataclass(frozen=True)
class PromptTemplate:
    key: str
    text: str
    version: str
    source_path: str

    def render(self, **kwargs: str) -> str:
        rendered = self.text
        for key, value in kwargs.items():
            rendered = rendered.replace(f"{{{key}}}", str(value))
        return rendered


class PromptStore:
    def __init__(self, prompt_dir: Path) -> None:
        self.prompt_dir = prompt_dir

    def get(self, key: str) -> PromptTemplate:
        path = self.prompt_dir / f"{key}.md"
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")
        text = path.read_text(encoding="utf-8").strip()
        version = sha1(text.encode("utf-8")).hexdigest()[:12]
        return PromptTemplate(key=key, text=text, version=version, source_path=str(path))

    def render(self, key: str, **kwargs: str) -> str:
        return self.get(key).render(**kwargs)

    def version(self, key: str) -> str:
        return self.get(key).version


@lru_cache
def get_prompt_store() -> PromptStore:
    settings = get_settings()
    prompt_dir = settings.chat_prompt_dir or Path(__file__).with_name("prompts")
    return PromptStore(prompt_dir)
