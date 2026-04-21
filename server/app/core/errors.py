from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "app_error") -> None:
        self.message = message
        self.status_code = status_code
        self.code = code
        super().__init__(message)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    response = JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
    attach_cors(request, response)
    return response


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    response = JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc)}},
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
