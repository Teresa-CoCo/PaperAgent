from fastapi import Header


def current_user_id(x_user_id: str | None = Header(default=None)) -> str:
    # Lightweight identity for local research use. Replace with JWT/session auth in production.
    return x_user_id or "local-user"

