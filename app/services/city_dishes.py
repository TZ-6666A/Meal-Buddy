"""AI 城市日常菜层：让模型基于训练语料里的本地美食内容，整理"当地人常点"的菜品。

定位与边界：
- 是本地特色菜（CITY_SPECIALTIES）和全国通用菜库之间的补充层，覆盖
  "川菜馆、猪脚饭、麻辣烫"这类当地日常馆子菜；
- 结果按城市进程内缓存，同一城市只调一次模型；
- 模型失败或未配置 Key 时静默返回空列表，推荐链路自动退回原有两层。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from openai import OpenAI

from app.config import get_settings
from app.prompts import PROMPT_VERSION, load
from app.services import usage

logger = logging.getLogger("fandazi.city_dishes")

_VALID_TASTE = {"salty", "spicy", "sweet", "light"}
_VALID_STAPLE = {"rice", "noodle", "nocarb", "any"}
_VALID_HEAVY = {"heavy", "simple", ""}
# 提示词已约束模型，这里再拦一道：套餐/饮品/营销词不是"菜"
_NOISE_WORDS = ("双人餐", "单人餐", "套餐", "优惠券", "代金券", "霸王餐", "秒杀",
                "咖啡", "美式", "拿铁", "摩卡", "果茶", "奶茶", "奶昔", "柠檬水",
                "可乐", "雪碧", "果汁", "饮品")
_TIMEOUT = 30.0  # 只在后台预热线程里跑，放宽换成功率
# 缓存条目保鲜期：菜品结构变化慢，8 小时足够
_TTL = 8 * 3600
_cache: dict[str, tuple[float, list[dict]]] = {}
_inflight: set[str] = set()
_lock = threading.Lock()


def _client(settings=None) -> OpenAI | None:
    settings = settings or get_settings()
    if not settings.openai_api_key:
        return None
    return OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=_TIMEOUT,
        max_retries=0,
    )


def _clean_city(city: str) -> str:
    """「商洛市 · 商州区」->「商洛市」，模型按城市回答更稳。"""
    return str(city or "").split("·", 1)[0].strip()


def _parse(text: str, city: str) -> list[dict]:
    """解析并校验模型输出；任何不合规条目直接丢弃。"""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not 2 <= len(name) <= 20 or name in seen:
            continue
        if any(word in name for word in _NOISE_WORDS):
            continue
        taste = str(item.get("taste") or "")
        staple = str(item.get("staple") or "")
        heavy = str(item.get("heaviness") or "")
        if taste not in _VALID_TASTE or staple not in _VALID_STAPLE or heavy not in _VALID_HEAVY:
            continue
        seen.add(name)
        out.append({
            "dish": name,
            "source": "ai-city",
            "region": city,
            "tastes": (taste,) if taste else (),
            "staples": (staple,) if staple != "any" else ("any",),
            "heaviness": heavy or None,
        })
    return out


def fetch_city_dishes(city: str, settings=None) -> list[dict]:
    """返回城市日常菜（place 字典形态，可直接并入 nearby 候选）。失败返回 []。"""
    key = _clean_city(city)
    if not key:
        return []
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]

    settings = settings or get_settings()
    if not settings.openai_api_key:
        return []
    client = _client(settings)
    if client is None:
        return []
    started = time.monotonic()
    dishes: list[dict] = []
    failure = ""
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": load("city_dishes")},
                {"role": "user", "content": f"用户所在城市：{key}"},
            ],
            temperature=0.4,
        )
        dishes = _parse(resp.choices[0].message.content or "", key)
        usage.record_usage(
            endpoint="city-dishes", model=settings.openai_model,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            ok=bool(dishes), price_in=settings.llm_price_per_1k_input,
            price_out=settings.llm_price_per_1k_output,
            duration_ms=int((time.monotonic() - started) * 1000),
            prompt_version=PROMPT_VERSION,
            failure_reason="" if dishes else "empty_or_invalid",
        )
    except Exception as exc:  # 网络失败/超时/解析异常都走静默降级
        failure = type(exc).__name__
        logger.warning("city_dishes_failed %s", failure)
        usage.record_usage(
            endpoint="city-dishes", model=settings.openai_model, ok=False,
            price_in=settings.llm_price_per_1k_input,
            price_out=settings.llm_price_per_1k_output,
            duration_ms=int((time.monotonic() - started) * 1000),
            prompt_version=PROMPT_VERSION, failure_reason=failure,
        )

    if dishes:
        with _lock:
            _cache[key] = (time.monotonic(), dishes)
    return dishes


def cached(city: str) -> list[dict]:
    """只读缓存，不发起模型调用。未预热或过期返回 []。"""
    key = _clean_city(city)
    if not key:
        return []
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    return []


def prefetch(city: str, settings=None) -> None:
    """后台线程预热缓存，绝不阻塞调用方；同城市并发去重。"""
    key = _clean_city(city)
    if not key:
        return
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if (hit and now - hit[0] < _TTL) or key in _inflight:
            return
        _inflight.add(key)

    def _run() -> None:
        try:
            fetch_city_dishes(city, settings)
        finally:
            with _lock:
                _inflight.discard(key)

    threading.Thread(target=_run, daemon=True).start()


def reset_cache() -> None:
    with _lock:
        _cache.clear()
        _inflight.clear()
