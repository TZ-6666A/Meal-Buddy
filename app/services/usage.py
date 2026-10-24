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
}


def _today() -> str:
    return date.today().isoformat()


def _roll() -> None:
    if _state["day"] != _today():
        _state.update(day=_today(), calls=0, failed=0, prompt_tokens=0,
                      completion_tokens=0, cost=0.0)


def record_usage(
    *,
    endpoint: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    ok: bool = True,
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> None:
    _roll()
    cost = prompt_tokens / 1000 * price_in + completion_tokens / 1000 * price_out
    _state["calls"] += 1
    if not ok:
        _state["failed"] += 1
    _state["prompt_tokens"] += prompt_tokens
    _state["completion_tokens"] += completion_tokens
    _state["cost"] += cost
    logger.info(
        "llm_usage endpoint=%s model=%s ok=%s prompt_tokens=%d completion_tokens=%d cost=%.6f",
        endpoint, model, ok, prompt_tokens, completion_tokens, cost,
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
    }


def reset() -> None:
    """测试用。"""
    _state.update(day=_today(), calls=0, failed=0, prompt_tokens=0,
                  completion_tokens=0, cost=0.0)
