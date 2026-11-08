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
    assert client.get("/api/v1/preferences").json() == []
    with client.testing_session() as db:
        from app.models import RecommendationFeedback
        assert db.query(RecommendationFeedback).count() == 1


def test_explicit_dish_reactions_are_saved_to_taste(client, dev_user_id):
    with client.testing_session() as db:
        session = DecisionSession(
            user_id=dev_user_id, mode="takeout", drawn=["红烧茄子", "番茄炒蛋"], finished=True
        )
        db.add(session)
        db.commit()
        session_id = session.id

    url = f"/api/v1/decisions/{session_id}/feedback"
    liked = client.post(
        url,
        json={"dish_name": "红烧茄子", "action": "accept", "preference": "like"},
    )
    disliked = client.post(
        url,
        json={"dish_name": "番茄炒蛋", "action": "reject", "preference": "dislike"},
    )

    assert liked.status_code == 200
    assert disliked.status_code == 200
    assert {(item["kind"], item["topic"]) for item in client.get("/api/v1/preferences").json()} == {
        ("like", "红烧茄子"),
        ("dislike", "番茄炒蛋"),
    }


def test_meal_pagination_caps_limit_and_offset_past_end(client):
    for index in range(4):
        client.post("/api/v1/meals", json={"description": f"记录{index}"})

    assert client.get("/api/v1/meals?limit=201").status_code == 422
    assert client.get("/api/v1/meals?offset=99").json() == []


def test_prune_only_removes_old_chat_and_finished_sessions(client, dev_user_id):
    old = datetime.now(timezone.utc) - timedelta(days=120)
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    with client.testing_session() as db:
        db.add_all([
            ChatMessage(user_id=dev_user_id, role="user", content="旧消息", created_at=old),
            ChatMessage(user_id=dev_user_id, role="agent", content="新消息", created_at=recent),
            DecisionSession(user_id=dev_user_id, mode="takeout", finished=True, created_at=old),
            DecisionSession(user_id=dev_user_id, mode="takeout", finished=False, created_at=old),
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
