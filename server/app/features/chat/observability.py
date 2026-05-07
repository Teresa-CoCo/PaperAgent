from dataclasses import dataclass, field

from app.core.logging import log_event


@dataclass
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    tool_calls: int = 0

    def add_usage(self, prompt_tokens: int, completion_tokens: int, total_tokens: int, estimated_cost_usd: float) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.estimated_cost_usd = round(self.estimated_cost_usd + estimated_cost_usd, 6)

    def add_tool_call(self, count: int = 1) -> None:
        self.tool_calls += count

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "tool_calls": self.tool_calls,
        }


@dataclass
class WorkflowTrace:
    workflow_id: str
    user_id: str
    session_id: str
    mode: str
    totals: UsageTotals = field(default_factory=UsageTotals)

    def log(self, event: str, **fields: object) -> None:
        log_event(
            "info",
            event,
            workflow_id=self.workflow_id,
            user_id=self.user_id,
            session_id=self.session_id,
            mode=self.mode,
            **fields,
        )
