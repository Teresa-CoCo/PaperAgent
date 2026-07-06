import re

from fastapi import Header, HTTPException


USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._@:-]{1,80}$")


def current_user_id(x_user_id: str | None = Header(default=None)) -> str:
    # Lightweight identity for local research use. Replace with JWT/session auth in production.
    user_id = (x_user_id or "local-user").strip()
    if not USER_ID_PATTERN.fullmatch(user_id):
        raise HTTPException(status_code=400, detail="Invalid X-User-Id")
    return user_id


def require_user_id(x_user_id: str | None = Header(default=None)) -> str:
    user_id = (x_user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="X-User-Id header required")
    if not USER_ID_PATTERN.fullmatch(user_id):
        raise HTTPException(status_code=400, detail="Invalid X-User-Id")
    return user_id
