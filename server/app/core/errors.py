from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import log_event


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "app_error") -> None:
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: str = "") -> None:
        super().__init__(f"{resource} not found{f': {resource_id}' if resource_id else ''}", 404, "not_found")


class ValidationError(AppError):
    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message, 422, "validation_error")
        self.field = field


class AuthError(AppError):
    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message, 401, "auth_required")


class ForbiddenError(AppError):
    def __init__(self, message: str = "Permission denied") -> None:
        super().__init__(message, 403, "forbidden")


class RateLimitError(AppError):
    def __init__(self, message: str = "Rate limit exceeded") -> None:
        super().__init__(message, 429, "rate_limit_exceeded")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    response = JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
    attach_cors(request, response)
    return response


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event("error", "unhandled_exception", path=request.url.path, error=str(exc), error_type=exc.__class__.__name__)
    response = JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": "Internal server error"}},
    )
    attach_cors(request, response)
    return response


def attach_cors(request: Request, response: JSONResponse) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    settings = get_settings()
    if origin in settings.cors_origin_list:
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
        response.headers["vary"] = "Origin"
