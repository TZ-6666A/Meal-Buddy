"""进程内 LLM 用量统计：当日调用次数、token、估算成本。

只用于单进程本地原型；多实例部署需要换成共享存储。
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger("fandazi.usage")

_state: dict = {
    "day": None,
    "calls": 0,
    "failed": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cost": 0.0,
    "last_duration_ms": 0,
    "last_failure": "",
}


def _today() -> str:
    return date.today().isoformat()


def _roll() -> None:
    if _state["day"] != _today():
        _state.update(day=_today(), calls=0, failed=0, prompt_tokens=0,
                      completion_tokens=0, cost=0.0, last_duration_ms=0, last_failure="")


def record_usage(
    *,
    endpoint: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    ok: bool = True,
    price_in: float = 0.0,
    price_out: float = 0.0,
    duration_ms: int = 0,
    prompt_version: str = "",
    failure_reason: str = "",
) -> None:
    """记录一次模型调用。

    只记录模型名、提示词版本、耗时、token 和失败类型；
    不记录密钥、完整提示词或用户原文。
    """
    _roll()
    cost = prompt_tokens / 1000 * price_in + completion_tokens / 1000 * price_out
    _state["calls"] += 1
    _state["last_duration_ms"] = duration_ms
    if not ok:
        _state["failed"] += 1
        _state["last_failure"] = f"{endpoint}:{failure_reason or 'unknown'}"[:120]
    _state["prompt_tokens"] += prompt_tokens
    _state["completion_tokens"] += completion_tokens
    _state["cost"] += cost
    logger.info(
        "llm_usage endpoint=%s model=%s prompt_version=%s ok=%s duration_ms=%d "
        "prompt_tokens=%d completion_tokens=%d cost=%.6f failure=%s",
        endpoint, model, prompt_version, ok, duration_ms,
        prompt_tokens, completion_tokens, cost, failure_reason or "-",
    )


def today_tokens() -> int:
    _roll()
    return _state["prompt_tokens"] + _state["completion_tokens"]


def snapshot() -> dict:
    _roll()
    return {
        "date": _state["day"],
        "calls": _state["calls"],
        "failed": _state["failed"],
        "prompt_tokens": _state["prompt_tokens"],
        "completion_tokens": _state["completion_tokens"],
        "total_tokens": _state["prompt_tokens"] + _state["completion_tokens"],
        "estimated_cost": round(_state["cost"], 6),
        "last_duration_ms": _state["last_duration_ms"],
        "last_failure": _state["last_failure"],
    }


def reset() -> None:
    """测试用。"""
    _state.update(day=_today(), calls=0, failed=0, prompt_tokens=0,
                  completion_tokens=0, cost=0.0, last_duration_ms=0, last_failure="")
