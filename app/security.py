import hmac
import ipaddress
import logging
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts import hash_password, hash_token
from app.config import get_settings
from app.db import get_db
from app.models import User, UserSession

logger = logging.getLogger("fandazi.security")

SESSION_COOKIE_NAME = "fandazi_session"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128")
)


def _is_private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def client_host(request: Request) -> str:
    """取真实客户端 IP。

    直连时就是 socket 对端地址；经 Caddy/nginx 反代时对端是内网代理地址，
    此时取 X-Forwarded-For 最左边那个公网地址。只有对端确实是内网地址才信任
    该头，避免外部请求自带伪造的 X-Forwarded-For 骗过限流和敏感接口的本机判断。
    """
    peer = request.client.host if request.client else ""
    if peer and not _is_private(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    for candidate in forwarded.split(","):
        hop = candidate.strip()
        if hop and not _is_private(hop):
            return hop
    return peer or "unknown"


def _reject(request: Request, detail: str) -> HTTPException:
    logger.warning("auth_rejected path=%s client=%s", request.url.path, client_host(request))
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _token_matches(request: Request) -> bool:
    """脚本通道：X-API-Token 与服务端配置一致时视为管理员身份。"""
    expected = get_settings().api_token
    if not expected:
        return False
    return hmac.compare_digest(request.headers.get("x-api-token", ""), expected)


def _user_from_cookie(db: Session, request: Request) -> User | None:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    stored = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw_token)))
    if stored is None:
        return None
    # SQLite 测试库回读的 datetime 不带时区，统一按 UTC 处理
    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    user = db.get(User, stored.user_id)
    if user is None or not user.is_active:
        return None
    return user


def _dev_user(db: Session) -> User:
    """开发/测试环境的免登录用户（管理员身份），不存在则懒创建。"""
    user = db.scalar(select(User).where(User.username == "dev"))
    if user is None:
        user = User(username="dev", password_hash=hash_password("dev12345"), is_admin=True)
        db.add(user)
        db.commit()
    return user


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """网页与脚本共用的身份入口：Cookie 会话 > X-API-Token > 开发旁路。"""
    user = _user_from_cookie(db, request)
    if user is not None:
        return user
    if _token_matches(request):
        admin = db.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1))
        if admin is not None and admin.is_active:
            return admin
        raise _reject(request, "服务尚未初始化管理员账号")
    settings = get_settings()
    if not settings.api_token and not settings.is_production and getattr(settings, "dev_auto_login", True):
        return _dev_user(db)
    raise _reject(request, "请先登录")


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def set_session_cookie(response: Response, raw_token: str) -> None:
    settings = get_settings()
    days = getattr(settings, "session_ttl_days", 30) or 30
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
