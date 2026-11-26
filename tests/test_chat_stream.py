import asyncio
import json
from types import SimpleNamespace

from app.services import meal_assistant, usage
from app.services.meal_assistant import DietAgent


def _chunk(text=None, tokens=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))] if text is not None else [],
        usage=SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]) if tokens else None,
    )


class FakeStreamClient:
    def __init__(self, chunks, fail_after=None):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.chunks = chunks
        self.fail_after = fail_after
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs

        async def gen():
            for index, chunk in enumerate(self.chunks):
                if self.fail_after is not None and index >= self.fail_after:
                    raise RuntimeError("upstream dropped")
                yield chunk

        return gen()


def _collect(message, notes=None):
    async def run():
        return [(kind, data) async for kind, data in DietAgent().chat_stream(message, notes or [])]

    return asyncio.run(run())


def test_stream_yields_deltas_then_final_and_records_usage(monkeypatch, fake_settings):
    client = FakeStreamClient([_chunk("番茄"), _chunk("豆腐汤"), _chunk("很合适。"), _chunk(tokens=(11, 7))])
    monkeypatch.setattr(meal_assistant, "get_chat_client", lambda settings: client)
    usage.reset()

    events = _collect("鸡蛋和豆腐怎么搭配")

    assert [data for kind, data in events if kind == "delta"] == ["番茄", "豆腐汤", "很合适。"]
    final = events[-1]
    assert final[0] == "final"
    assert final[1]["reply"] == "番茄豆腐汤很合适。"
    assert final[1]["source"] == "llm"
    assert client.kwargs["stream"] is True
    assert client.kwargs["stream_options"] == {"include_usage": True}
    snapshot = usage.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["failed"] == 0
    assert snapshot["prompt_tokens"] == 11
    assert snapshot["completion_tokens"] == 7


def test_stream_overlong_reply_is_replaced_by_fallback(monkeypatch, fake_settings):
    client = FakeStreamClient([_chunk("长" * 401), _chunk(tokens=(5, 401))])
    monkeypatch.setattr(meal_assistant, "get_chat_client", lambda settings: client)
    usage.reset()

    events = _collect("鸡蛋怎么做好吃")

    assert events[0][0] == "delta"
    final = events[-1][1]
    assert final["source"] == "fallback"
    assert final["reply"] == DietAgent._fallback("鸡蛋怎么做好吃")
    assert usage.snapshot()["failed"] == 1


def test_stream_fabricated_allergy_is_replaced_by_fallback(monkeypatch, fake_settings):
    client = FakeStreamClient([_chunk("你对花生过敏，别吃这个。"), _chunk(tokens=(6, 9))])
    monkeypatch.setattr(meal_assistant, "get_chat_client", lambda settings: client)
    usage.reset()

    final = _collect("鸡蛋怎么做好吃")[-1][1]

    assert final["source"] == "fallback"
    assert usage.snapshot()["failed"] == 1


def test_stream_midway_failure_records_and_falls_back(monkeypatch, fake_settings):
    client = FakeStreamClient([_chunk("番茄"), _chunk("豆腐汤")], fail_after=1)
    monkeypatch.setattr(meal_assistant, "get_chat_client", lambda settings: client)
    usage.reset()

    events = _collect("鸡蛋和豆腐怎么搭配")

    assert events[0] == ("delta", "番茄")
    final = events[-1][1]
    assert final["source"] == "fallback"
    assert final["reply"] == DietAgent._fallback("鸡蛋和豆腐怎么搭配")
    snapshot = usage.snapshot()
    assert snapshot["failed"] == 1
    assert snapshot["last_failure"].startswith("chat:")


def test_stream_rule_paths_never_call_model(monkeypatch, fake_settings):
    def forbidden(settings):
        raise AssertionError("rule path must not build a client")

    monkeypatch.setattr(meal_assistant, "get_chat_client", forbidden)
    usage.reset()

    preference = _collect("我不吃香菜")
    assert preference == [("final", {"reply": "", "learned": preference[0][1]["learned"], "source": "rule"})]
    assert preference[0][1]["learned"]

    off_topic = _collect("今天股市怎么样")
    assert off_topic[-1][1]["source"] == "rule"
    assert off_topic[-1][1]["reply"] == meal_assistant.OFF_TOPIC_REPLY
    assert usage.snapshot()["calls"] == 0


def test_chat_stream_endpoint_streams_and_persists(client, monkeypatch, fake_settings):
    fake = FakeStreamClient([_chunk("清汤面"), _chunk("不错。"), _chunk(tokens=(9, 5))])
    monkeypatch.setattr(meal_assistant, "get_chat_client", lambda settings: fake)
    usage.reset()

    res = client.post("/api/v1/chat/stream", json={"message": "今晚想吃得清淡点"})

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[5:].strip())
        for line in res.text.split("\n\n") if line.startswith("data:")
    ]
    deltas = [evt["text"] for evt in events if evt["stage"] == "delta"]
    done = [evt for evt in events if evt["stage"] == "done"]
    assert deltas == ["清汤面", "不错。"]
    assert len(done) == 1
    assert done[0]["reply"] == "清汤面不错。"
    assert done[0]["source"] == "llm"

    history = client.get("/api/v1/chat/history").json()
    contents = [item["content"] for item in history]
    assert "今晚想吃得清淡点" in contents
    assert "清汤面不错。" in contents