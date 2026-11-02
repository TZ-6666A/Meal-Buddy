"""小饭教你做饭：模型返回结构化菜谱，失败时用本地库兜底。忌口提醒不依赖模型。"""

from __future__ import annotations

import json
import logging
import time

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import get_settings
from app.prompts import PROMPT_VERSION, load
from app.schemas import CookIngredient, CookLesson, CookStep
from app.services import usage
from app.services.cook_lessons import generic_recipe, lookup_recipe
from app.services.decision import BLOCK_ALIASES, expand_blocked

logger = logging.getLogger("fandazi.cook")

# 命中忌口时给一个能替换的常见食材，避免只说「不要吃」却不给做法。
_SWAP = {
    "牛肉": "鸡腿肉",
    "牛腩": "鸡腿肉",
    "牛排": "鸡腿肉",
    "肥牛": "鸡腿肉",
    "牛柳": "鸡腿肉",
    "猪肉": "鸡腿肉",
    "五花": "鸡腿肉",
    "排骨": "鸡腿肉",
    "里脊": "鸡胸肉",
    "鸡肉": "豆腐",
    "鸡胸": "豆腐",
    "鸡腿": "豆腐",
    "鸡翅": "豆腐",
    "羊肉": "牛肉",
    "鱼": "豆腐",
    "虾": "鸡胸肉",
    "海鲜": "鸡胸肉",
    "鸡蛋": "嫩豆腐",
    "蛋": "嫩豆腐",
    "花生": "熟腰果",
    "香菜": "小葱",
    "葱": "小葱叶少放",
    "蒜": "姜",
    "牛奶": "无糖豆浆",
}


def _swap_for(name: str) -> str:
    if name in _SWAP:
        return _SWAP[name]
    for key, value in _SWAP.items():
        if key in name:
            return value
    return "另一种你能吃的同类食材"


def build_warnings(ingredients: list[CookIngredient], topics: list[str]) -> list[str]:
    """一条忌口记录提醒一次。用 expand_blocked 展开后再匹配，不能只做全等。"""
    warnings: list[str] = []
    names = [item.name.strip() for item in ingredients if item.name and item.name.strip()]
    claimed: set[str] = set()
    for raw in topics or []:
        topic = (raw or "").strip()
        if not topic:
            continue
        expanded = [word for word in expand_blocked([topic]) if word]
        aliases = [alias for alias in BLOCK_ALIASES.get(topic, ()) if alias]
        # 别名放前面：用户记的是「牛肉」时，要先命中牛腩、肥牛，而不是被原词漏掉。
        needles = aliases + [word for word in expanded if word not in aliases]
        if not needles:
            needles = [topic]
        hit = ""
        matched = ""
        for needle in needles:
            for name in names:
                if name in claimed:
                    continue
                if needle and needle in name:
                    hit = name
                    matched = needle
                    break
            if hit:
                break
        if not hit:
            continue
        claimed.add(hit)
        # 句子里点名用户记过的那条；命中的是别名时，食材名用菜谱里的写法。
        label = topic if matched == topic or matched in BLOCK_ALIASES.get(topic, ()) else matched
        swap = _swap_for(hit)
        warnings.append(f"你记过不能吃{label}，这道菜用到{hit}，可以换{swap}或直接去掉。")
    return warnings


def _fallback_lesson(dish: str) -> CookLesson:
    data = lookup_recipe(dish) or generic_recipe(dish)
    return CookLesson.model_validate(data)


def _client(settings) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=40.0,
        max_retries=0,
    )


def _record(completion, settings, ok: bool, started: float, failure_reason: str = "") -> None:
    prompt_tokens = completion_tokens = 0
    if completion is not None and getattr(completion, "usage", None) is not None:
        prompt_tokens = getattr(completion.usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(completion.usage, "completion_tokens", 0) or 0
    usage.record_usage(
        endpoint="cook_lesson",
        model=settings.openai_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        ok=ok,
        price_in=getattr(settings, "llm_price_per_1k_input", 0.0),
        price_out=getattr(settings, "llm_price_per_1k_output", 0.0),
        duration_ms=int((time.perf_counter() - started) * 1000),
        prompt_version=PROMPT_VERSION,
        failure_reason=failure_reason,
    )


class CookTeacher:
    async def teach(self, dish: str, blocked_topics: list[str] | None = None) -> CookLesson:
        settings = get_settings()
        lesson = await self._from_model(dish) if settings.openai_api_key else None
        if lesson is None:
            lesson = _fallback_lesson(dish)
        lesson.warnings = build_warnings(lesson.ingredients, list(blocked_topics or []))
        return lesson

    async def _from_model(self, dish: str) -> CookLesson | None:
        settings = get_settings()
        client = _client(settings)
        completion = None
        started = time.perf_counter()
        try:
            completion = await client.chat.completions.create(
                model=settings.openai_model,
                temperature=0.4,
                max_tokens=1800,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": load("teach_cook")},
                    {"role": "user", "content": json.dumps({"dish": dish, "servings": "2人份"}, ensure_ascii=False)},
                ],
            )
            content = completion.choices[0].message.content or "{}"
            data = json.loads(content)
            lesson = CookLesson.model_validate(data)
            if not lesson.ingredients or not lesson.steps:
                raise ValueError("模型没有给出食材或步骤")
            lesson.dish = lesson.dish or dish
            lesson.servings = "2人份"
            _record(completion, settings, ok=True, started=started)
            return lesson
        except (ValidationError, ValueError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            logger.exception("cook_lesson_invalid model=%s prompt_version=%s",
                             settings.openai_model, PROMPT_VERSION)
            _record(completion, settings, ok=False, started=started, failure_reason=type(exc).__name__)
            return None
        except Exception as exc:
            logger.exception("cook_lesson_failed model=%s prompt_version=%s",
                             settings.openai_model, PROMPT_VERSION)
            _record(completion, settings, ok=False, started=started, failure_reason=type(exc).__name__)
            return None
