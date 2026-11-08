from app.models import ChatMessage
from app.services.meal_assistant import build_chat_messages


def test_chat_history_returns_role_content_and_chronological_order(client, dev_user_id):
    with client.testing_session() as db:
        db.add_all([
            ChatMessage(user_id=dev_user_id, role="user", content="先问一句"),
            ChatMessage(user_id=dev_user_id, role="agent", content="先答一句"),
        ])
        db.commit()

    response = client.get("/api/v1/chat/history")

    assert response.status_code == 200
    assert [(message["role"], message["content"]) for message in response.json()] == [
        ("user", "先问一句"),
        ("agent", "先答一句"),
    ]


def test_chat_context_keeps_six_recent_messages_and_summarizes_older(client, dev_user_id, monkeypatch):
    captured = {}

    async def fake_chat(message, notes, history=None, earlier_summary=""):
        captured["history"] = history
        captured["summary"] = earlier_summary
        captured["model_messages"] = build_chat_messages(
            message, notes, history, earlier_summary
        )
        return "收到", [], "rule"

    monkeypatch.setattr("app.api.agent.chat", fake_chat)
    with client.testing_session() as db:
        db.add_all([
            ChatMessage(user_id=dev_user_id, role="user" if i % 2 == 0 else "agent", content=f"早期想吃豆腐 第{i}")
            for i in range(10)
        ])
        db.commit()

    response = client.post("/api/v1/chat", json={"message": "接着聊"})

    assert response.status_code == 200
    assert len(captured["history"]) == 6
    assert captured["history"][0]["content"].endswith("第4")
    assert captured["history"][-1]["content"].endswith("第9")
    assert "豆腐" in captured["summary"]
    model_messages = captured["model_messages"]
    assert model_messages[2]["role"] == "system"
    assert model_messages[2]["content"].startswith("更早的对话摘要：")
    assert len(model_messages[2]["content"]) <= 200
    assert [m["content"] for m in model_messages[-7:-1]] == [
        item["content"] for item in captured["history"]
    ]


def test_preference_statement_is_remembered_but_not_shown_in_history(client):
    response = client.post("/api/v1/chat", json={"message": "我不太喜欢吃香菜"})

    assert response.status_code == 200
    data = response.json()
    assert data["reply"] == ""
    assert [(item["kind"], item["topic"]) for item in data["learned"]] == [("dislike", "香菜")]
    assert client.get("/api/v1/preferences").json()
    assert client.get("/api/v1/chat/history").json() == []


def test_question_still_gets_a_reply_and_shows_in_history(client):
    response = client.post("/api/v1/chat", json={"message": "今天吃什么"})

    assert response.status_code == 200
    assert response.json()["reply"]
    contents = [m["content"] for m in client.get("/api/v1/chat/history").json()]
    assert "今天吃什么" in contents
