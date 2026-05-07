import json
import uuid

from app.db.connection import transaction


class ChatWorkflowStore:
    def create_workflow_run(self, user_id: str, session_id: str, mode: str, message: str) -> str:
        workflow_id = str(uuid.uuid4())
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_workflow_runs(id, user_id, session_id, mode, status, message)
                VALUES(?, ?, ?, ?, 'running', ?)
                """,
                (workflow_id, user_id, session_id, mode, message),
            )
        return workflow_id

    def mark_workflow_context(self, workflow_id: str, *, classification: dict, prompt_versions: dict) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE chat_workflow_runs
                SET classification_json = ?, prompt_versions_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    json.dumps(classification, ensure_ascii=False),
                    json.dumps(prompt_versions, ensure_ascii=False),
                    workflow_id,
                ),
            )

    def finish_workflow_run(self, workflow_id: str, *, status: str, metrics: dict, error_message: str | None = None) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE chat_workflow_runs
                SET status = ?, metrics_json = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, json.dumps(metrics, ensure_ascii=False), error_message, workflow_id),
            )

    def start_agent_run(self, workflow_id: str, *, agent_key: str, agent_name: str, phase: str, prompt_version: str) -> int:
        with transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO chat_agent_runs(workflow_id, agent_key, agent_name, phase, status, prompt_version)
                VALUES(?, ?, ?, ?, 'running', ?)
                """,
                (workflow_id, agent_key, agent_name, phase, prompt_version),
            )
            return int(cursor.lastrowid)

    def finish_agent_run(
        self,
        run_id: int,
        *,
        status: str,
        attempt_count: int,
        duration_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float,
        tool_call_count: int,
        metadata: dict,
        error_message: str | None = None,
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                UPDATE chat_agent_runs
                SET status = ?, attempt_count = ?, duration_ms = ?, input_tokens = ?, output_tokens = ?, total_tokens = ?,
                    estimated_cost_usd = ?, tool_call_count = ?, metadata_json = ?, error_message = ?,
                    updated_at = CURRENT_TIMESTAMP, finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    status,
                    attempt_count,
                    duration_ms,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    tool_call_count,
                    json.dumps(metadata, ensure_ascii=False),
                    error_message,
                    run_id,
                ),
            )

    def record_tool_run(
        self,
        workflow_id: str,
        *,
        agent_key: str,
        tool_call_id: str,
        tool_name: str,
        status: str,
        duration_ms: int,
        arguments_json: str,
        result_json: dict,
        approval_required: bool = False,
        error_message: str | None = None,
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_tool_runs(
                  workflow_id, agent_key, tool_call_id, tool_name, status, duration_ms,
                  approval_required, arguments_json, result_json, error_message
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    agent_key,
                    tool_call_id,
                    tool_name,
                    status,
                    duration_ms,
                    1 if approval_required else 0,
                    arguments_json,
                    json.dumps(result_json, ensure_ascii=False),
                    error_message,
                ),
            )

    def daily_usage(self, user_id: str, usage_date: str) -> dict:
        with transaction() as connection:
            row = connection.execute(
                "SELECT * FROM chat_daily_usage WHERE user_id = ? AND usage_date = ?",
                (user_id, usage_date),
            ).fetchone()
        return row or {
            "user_id": user_id,
            "usage_date": usage_date,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tool_calls": 0,
            "estimated_cost_usd": 0.0,
        }

    def increment_daily_usage(
        self,
        user_id: str,
        usage_date: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        tool_calls: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO chat_daily_usage(
                  user_id, usage_date, prompt_tokens, completion_tokens, total_tokens, tool_calls, estimated_cost_usd
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, usage_date) DO UPDATE SET
                  prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                  completion_tokens = completion_tokens + excluded.completion_tokens,
                  total_tokens = total_tokens + excluded.total_tokens,
                  tool_calls = tool_calls + excluded.tool_calls,
                  estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, usage_date, prompt_tokens, completion_tokens, total_tokens, tool_calls, estimated_cost_usd),
            )
