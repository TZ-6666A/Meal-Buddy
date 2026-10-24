from types import SimpleNamespace

from app.rate_limit import reset_rate_limits
from app.schemas import RecommendationResponse


def test_recommendation_rate_limit_returns_429_on_eleventh_request(client, monkeypatch):
    from app import api
    # Replace the function imported by the limiter; no model or database call is made.
    monkeypatch.setattr(
        "app.rate_limit.get_settings", lambda: SimpleNamespace(rate_limit_per_minute=10)
    )

    async def fake_recommendation(payload, db):
        return RecommendationResponse(explanation="test", recipes=[], source="demo")

    monkeypatch.setattr(api, "_build_recommendation", fake_recommendation)
    reset_rate_limits()
    statuses = [
        client.post("/api/v1/recommendations", json={}).status_code
        for _ in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429
