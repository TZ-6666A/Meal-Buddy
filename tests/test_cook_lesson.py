from __future__ import annotations

import pytest

from app.services.cook_lessons import LOCAL_RECIPES
from app.services.cook_teacher import CookTeacher


@pytest.fixture()
def offline_model(monkeypatch):
    """固定模型不可用，测试不触网。"""
    monkeypatch.setattr(
        "app.services.cook_teacher.get_settings",
        lambda: type("S", (), {
            "openai_api_key": "",
            "openai_base_url": "http://127.0.0.1:1/v1",
            "openai_model": "test-model",
            "llm_price_per_1k_input": 0.0,
            "llm_price_per_1k_output": 0.0,
        })(),
    )


def test_tomato_egg_without_key_has_steps_and_caution(client, offline_model):
    response = client.post("/api/v1/cook-lessons", json={"dish": "番茄炒蛋"})

    assert response.status_code == 200
    data = response.json()
    assert data["ingredients"]
    assert len(data["steps"]) >= 4
    assert any((step.get("caution") or "").strip() for step in data["steps"])
    assert data["warnings"] == []


def test_allergy_names_the_ingredient_and_miss_stays_empty(client, offline_model):
    client.post("/api/v1/preferences", json={"kind": "avoid", "topic": "牛肉", "text": "不吃牛肉"})

    hit = client.post("/api/v1/cook-lessons", json={"dish": "番茄牛腩"})
    assert hit.status_code == 200
    warnings = hit.json()["warnings"]
    assert warnings
    assert any("牛腩" in text and "牛肉" in text for text in warnings)

    miss = client.post("/api/v1/cook-lessons", json={"dish": "番茄炒蛋"})
    assert miss.status_code == 200
    assert miss.json()["warnings"] == []


def test_unknown_dish_still_returns_steps(client, offline_model):
    dish = "星云炒月"
    assert dish not in LOCAL_RECIPES

    response = client.post("/api/v1/cook-lessons", json={"dish": dish})

    assert response.status_code == 200
    data = response.json()
    assert data["steps"]
    assert "通用做法" in data["note"]


def test_blank_dish_is_422(client, offline_model):
    response = client.post("/api/v1/cook-lessons", json={"dish": "   "})
    assert response.status_code == 422


def test_model_failure_uses_local_recipe(offline_model, monkeypatch):
    class _Completions:
        @staticmethod
        async def create(**kwargs):
            raise RuntimeError("network down")

    class _Chat:
        completions = _Completions()

    class _Boom:
        def __init__(self, **kwargs):
            self.chat = _Chat()

    monkeypatch.setattr("app.services.cook_teacher.AsyncOpenAI", _Boom)
    monkeypatch.setattr(
        "app.services.cook_teacher.get_settings",
        lambda: type("S", (), {
            "openai_api_key": "test-key",
            "openai_base_url": "http://127.0.0.1:1/v1",
            "openai_model": "test-model",
            "llm_price_per_1k_input": 0.0,
            "llm_price_per_1k_output": 0.0,
        })(),
    )

    import asyncio

    lesson = asyncio.run(CookTeacher().teach("宫保鸡丁", ["花生"]))
    assert len(lesson.steps) >= 4
    assert lesson.warnings
    assert any("花生" in text for text in lesson.warnings)
