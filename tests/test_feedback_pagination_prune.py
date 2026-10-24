from datetime import datetime, timedelta, timezone

from app.models import ChatMessage, DecisionSession, MealEntry


def _drawn_session(client) -> tuple[int, str]:
    session = client.post("/api/v1/decisions?mode=takeout").json()
    drawn = client.post(f"/api/v1/decisions/{session['session_id']}/again").json()
    with client.testing_session() as db:
        record = db.get(DecisionSession, session["session_id"])
        return record.id, record.drawn[-1]


def test_feedback_rejects_unknown_dish_and_updates_duplicate(client):
    session_id, dish_name = _drawn_session(client)
    invalid = client.post(
        f"/api/v1/decisions/{session_id}/feedback",
        json={"dish_name": "不是本次推荐的菜", "action": "accept"},
    )
    assert invalid.status_code == 422

    url = f"/api/v1/decisions/{session_id}/feedback"
    first = client.post(url, json={"dish_name": dish_name, "action": "accept"})
    second = client.post(url, json={"dish_name": dish_name, "action": "reject", "rating": 2})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["action"] == "reject"
    with client.testing_session() as db:
        from app.models import RecommendationFeedback
        assert db.query(RecommendationFeedback).count() == 1


def test_meal_pagination_caps_limit_and_offset_past_end(client):
    for index in range(4):
        client.post("/api/v1/meals", json={"description": f"记录{index}"})

    assert client.get("/api/v1/meals?limit=201").status_code == 422
    assert client.get("/api/v1/meals?offset=99").json() == []


def test_prune_only_removes_old_chat_and_finished_sessions(client):
    old = datetime.now(timezone.utc) - timedelta(days=120)
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    with client.testing_session() as db:
        db.add_all([
            ChatMessage(role="user", content="旧消息", created_at=old),
            ChatMessage(role="agent", content="新消息", created_at=recent),
            DecisionSession(mode="takeout", finished=True, created_at=old),
            DecisionSession(mode="takeout", finished=False, created_at=old),
        ])
        db.commit()

    response = client.post("/api/v1/maintenance/prune", json={"days": 90})

    assert response.status_code == 200
    assert response.json()["chat_messages"] == 1
    assert response.json()["decision_sessions"] == 1
    with client.testing_session() as db:
        assert db.query(ChatMessage).count() == 1
        sessions = db.query(DecisionSession).all()
        assert len(sessions) == 1 and sessions[0].finished is False
