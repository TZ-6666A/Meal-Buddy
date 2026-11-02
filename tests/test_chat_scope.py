from __future__ import annotations

import asyncio

import pytest

from app.prompts import load
from app.services.meal_assistant import OFF_TOPIC_REPLY, DietAgent


@pytest.mark.parametrize("message", [
    "你好",
    "晚上好",
    "在吗",
    "番茄能放多久",
    "鸡蛋和豆腐怎么搭配",
    "青椒肉丝怎么做",
    "今晚吃什么",
    "冰箱里还有剩饭，怎么热",
])
def test_food_and_greetings_stay_in_scope(message):
    assert DietAgent.in_food_scope(message)


@pytest.mark.parametrize("message", [
    "HTTP协议是什么",
    "怎么在国内用上chatgpt",
    "帮我写一段 python",
    "明天天气怎么样",
    "你好用的编辑器是哪个",
])
def test_unrelated_questions_are_out_of_scope(message):
    assert not DietAgent.in_food_scope(message)


def test_out_of_scope_never_reaches_the_model(monkeypatch):
    called = False

    async def fail_polish(self, message, notes, history=None, earlier_summary=""):
        nonlocal called
        called = True
        return "不该出现"

    monkeypatch.setattr(DietAgent, "_polish", fail_polish)
    reply, learned, source = asyncio.run(DietAgent().chat("HTTP协议是什么", []))

    assert reply == OFF_TOPIC_REPLY
    assert learned == []
    assert source == "rule"
    assert not called


def test_system_prompt_states_the_fixed_reply():
    prompt = load("chat_system")

    assert OFF_TOPIC_REPLY in prompt
    assert "打招呼" in prompt
