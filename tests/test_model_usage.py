import asyncio
from types import SimpleNamespace

import pytest

from app.schemas import RecommendationRequest
from app.services import usage
from app.services.meal_assistant import DietAgent


class FakeCompletionClient:
    def __init__(self, completion):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.completion = completion

    async def create(self, **kwargs):
        return self.completion


def test_invalid_recommendation_json_records_single_failed_call(monkeypatch, fake_settings):
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
        usage=SimpleNamespace(prompt_tokens=17, completion_tokens=9),
    )
    monkeypatch.setattr(
        "app.services.meal_assistant.get_recommend_client",
        lambda settings: FakeCompletionClient(completion),
    )
    usage.reset()

    with pytest.raises(RuntimeError, match="模型返回内容格式不正确"):
        asyncio.run(DietAgent()._request_recipes(RecommendationRequest(), [], [], [], []))

    snapshot = usage.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["prompt_tokens"] == 17
    assert snapshot["completion_tokens"] == 9
    assert snapshot["last_duration_ms"] >= 0
    assert snapshot["last_failure"].startswith("recommend:")


def test_chat_uses_model_and_records_success(monkeypatch, fake_settings):
    from app.services import meal_assistant

    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="番茄豆腐汤很合适。"))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )
    monkeypatch.setattr(
        meal_assistant,
        "get_chat_client",
        lambda settings: FakeCompletionClient(completion),
    )
    usage.reset()

    reply, learned, source = asyncio.run(
        DietAgent().chat("鸡蛋和豆腐怎么搭配", [])
    )

    assert reply == "番茄豆腐汤很合适。"
    assert learned == []
    assert source == "llm"
    snapshot = usage.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["failed"] == 0
    assert snapshot["prompt_tokens"] == 11
    assert snapshot["completion_tokens"] == 7


def test_chat_without_key_returns_fallback_and_logs_configuration_gap(
    monkeypatch, caplog
):
    from types import SimpleNamespace
    from app.services import meal_assistant

    monkeypatch.setattr(
        meal_assistant,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="",
            openai_model="test-model",
        ),
    )
    usage.reset()

    with caplog.at_level("WARNING", logger="fandazi.agent"):
        reply, learned, source = asyncio.run(
            DietAgent().chat("鸡蛋和豆腐怎么搭配", [])
        )

    assert reply == DietAgent._fallback("鸡蛋和豆腐怎么搭配")
    assert learned == []
    assert source == "fallback"
    assert usage.snapshot()["calls"] == 0
    assert "chat_llm_unconfigured" in caplog.text
    assert "test-model" in caplog.text


def test_chat_model_failure_returns_fallback_and_records_failure(
    monkeypatch, fake_settings, caplog
):
    from app.services import meal_assistant

    class FailingClient:
        chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: None,
            )
        )

        async def _create(self, **kwargs):
            raise RuntimeError("upstream unavailable")

    failing = FailingClient()
    failing.chat.completions.create = failing._create
    monkeypatch.setattr(meal_assistant, "get_chat_client", lambda settings: failing)
    usage.reset()

    with caplog.at_level("ERROR", logger="fandazi.agent"):
        reply, learned, source = asyncio.run(
            DietAgent().chat("鸡蛋和豆腐怎么搭配", [])
        )

    assert reply == DietAgent._fallback("鸡蛋和豆腐怎么搭配")
    assert learned == []
    assert source == "fallback"
    snapshot = usage.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["last_failure"].startswith("chat:RuntimeError")
    assert "upstream unavailable" in caplog.text
    assert "test-key" not in caplog.text


def test_model_clients_are_reused(monkeypatch, fake_settings):
    from app.services import meal_assistant

    created = []

    class FakeClient:
        def __init__(self, **kwargs):
            created.append(self)
            self.config = kwargs

    monkeypatch.setattr(meal_assistant, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(meal_assistant, "_chat_client", None)
    monkeypatch.setattr(meal_assistant, "_recommend_client", None)

    chat_first = meal_assistant.get_chat_client(fake_settings)
    chat_second = meal_assistant.get_chat_client(fake_settings)
    recommend_first = meal_assistant.get_recommend_client(fake_settings)
    recommend_second = meal_assistant.get_recommend_client(fake_settings)

    assert chat_first is chat_second
    assert recommend_first is recommend_second
    assert chat_first is not recommend_first
    assert len(created) == 2
    assert created[0].config["timeout"] == 20.0
    assert created[1].config["timeout"] == 75.0
