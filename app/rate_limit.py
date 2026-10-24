from collections import defaultdict, deque
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request, status

from app.config import get_settings

_WINDOW_SECONDS = 60.0
_requests: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def limit_model_calls(request: Request) -> None:
    limit = get_settings().rate_limit_per_minute
    if limit <= 0:
        return

    client_ip = request.client.host if request.client else "unknown"
    now = monotonic()
    with _lock:
        timestamps = _requests[client_ip]
        while timestamps and now - timestamps[0] >= _WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="操作太频繁了，歇一分钟再试。",
            )
        timestamps.append(now)


def reset_rate_limits() -> None:
    with _lock:
        _requests.clear()
