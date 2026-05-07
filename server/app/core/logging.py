import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from typing import Any

from fastapi import Request, Response


logging.basicConfig(
    level=logging.INFO,
    format='{"level":"%(levelname)s","time":"%(asctime)s","message":"%(message)s"}',
)
logger = logging.getLogger("paper-agent")
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def current_request_id() -> str:
    return _request_id_var.get()


def log_event(level: str, event: str, **fields: Any) -> None:
    payload = {
        "event": event,
        "request_id": current_request_id() or None,
        **fields,
    }
    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    getattr(logger, level.lower(), logger.info)(message)


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    token = _request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        log_event(
            "info",
            "http_request",
            method=request.method,
            path=request.url.path,
            duration_ms=duration_ms,
        )
        _request_id_var.reset(token)
    response.headers["x-request-id"] = request_id
    return response


async def security_headers_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["x-frame-options"] = "DENY"
    response.headers["referrer-policy"] = "strict-origin-when-cross-origin"
    return response
