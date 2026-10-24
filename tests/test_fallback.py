from __future__ import annotations

from app.schemas import Recipe
from app.services.meal_assistant import DietAgent


def test_recommend_falls_back_to_demo_when_model_fails(client, fake_settings, monkeypatch):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(DietAgent, "_request_recipes", boom)

    response = client.post(
        "/api/v1/recommendations",
        json={"ingredients": ["番茄"], "available_minutes": 30},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "fallback"
    assert data["recipes"]
    assert data["prompt_version"]


def test_fallback_never_returns_blocked_recipes(client, fake_settings, monkeypatch):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(DietAgent, "_request_recipes", boom)
    client.post("/api/v1/preferences", json={"kind": "allergy", "topic": "鸡蛋", "text": "对鸡蛋过敏"})

    response = client.post(
        "/api/v1/recommendations",
        json={"ingredients": [], "available_minutes": 30},
    )

    assert response.status_code == 200
    for recipe in response.json()["recipes"]:
        texts = [recipe["name"]] + [i["name"] for i in recipe["ingredients"]]
        assert not any("鸡蛋" in text for text in texts)


def test_recommend_stream_emits_stages(client, fake_settings, monkeypatch):
    async def fake_recipes(self, *args, **kwargs):
        return "好的", [
            Recipe(
                name="番茄炒蛋",
                description="快手",
                prep_minutes=10,
                ingredients=[{"name": "番茄", "amount": "1 个"}],
                steps=["炒熟"],
                tags=[],
            )
        ]

    monkeypatch.setattr(DietAgent, "_request_recipes", fake_recipes)

    with client.stream(
        "POST", "/api/v1/recommendations/stream",
        json={"ingredients": ["番茄"], "available_minutes": 30},
    ) as response:
        body = "".join(response.iter_text())

    assert '"stage": "accepted"' in body
    assert '"stage": "thinking"' in body
    assert '"stage": "done"' in body
    assert "番茄炒蛋" in body
