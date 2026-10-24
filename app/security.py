import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_token(x_api_token: str = Header(default="", alias="X-API-Token")) -> None:
    expected = get_settings().api_token
    if not expected:
        return
    if not hmac.compare_digest(x_api_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未授权")
