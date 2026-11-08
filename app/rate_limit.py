from collections import defaultdict, deque
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.security import client_host

_WINDOW_SECONDS = 60.0
_requests: dict[str, deque[float]] = defaultdict(deque)
_auth_requests: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def _consume(bucket: dict[str, deque[float]], key: str, limit: int, message: str) -> None:
    now = monotonic()
    with _lock:
        timestamps = bucket[key]
        while timestamps and now - timestamps[0] >= _WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=message)
        timestamps.append(now)


def limit_model_calls(request: Request) -> None:
    limit = get_settings().rate_limit_per_minute
    if limit <= 0:
        return

    # 反代后 request.client 是代理地址，不取真实 IP 会让所有用户共用一个桶。
    client_ip = client_host(request)
    _consume(
        _requests,
        client_ip,
        limit,
        "操作太频繁了，歇一分钟再试。",
    )


def limit_auth_calls(request: Request) -> None:
    """登录/注册接口的独立限流，防止在线爆破密码。"""
    limit = getattr(get_settings(), "auth_rate_limit_per_minute", 5)
    if limit <= 0:
        return
    _consume(
        _auth_requests,
        client_host(request),
        limit,
        "尝试太频繁了，请一分钟后再试。",
    )


def reset_rate_limits() -> None:
    with _lock:
        _requests.clear()
        _auth_requests.clear()
