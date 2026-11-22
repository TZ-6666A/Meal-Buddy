"""AI 城市日常菜层：解析校验、缓存与静默降级行为。"""
from types import SimpleNamespace
import threading

import pytest

from app.services import city_dishes


def _settings():
    return SimpleNamespace(
        openai_api_key="test-key",
        openai_base_url="http://localhost:1/v1",
        openai_model="test-model",
        llm_price_per_1k_input=0.0,
        llm_price_per_1k_output=0.0,
    )


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 40


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeClient:
    def __init__(self, content=None, error=None):
        self._content = content
        self._error = error
        self.calls = 0
        self.chat = self

    @property
    def completions(self):
        return self

    def close(self):
        pass

    def create(self, **kwargs):
        self.calls += 1
        if self._error:
            raise self._error
        return _Resp(self._content)


def test_parse_keeps_valid_entries_and_drops_bad_ones():
    text = '''
    前缀说明文字
    [{"name": "隆江猪脚饭", "taste": "salty", "staple": "rice", "heaviness": "heavy"},
     {"name": "x", "taste": "salty", "staple": "rice", "heaviness": "heavy"},
     {"name": "麻辣烫", "taste": "变态辣", "staple": "rice", "heaviness": "heavy"},
     {"name": "麻辣烫", "taste": "spicy", "staple": "rice", "heaviness": "heavy"},
     {"name": "双人餐", "taste": "salty", "staple": "rice", "heaviness": "heavy"},
     "不是字典的条目"]
    '''
    dishes = city_dishes._parse(text, "商洛市")
    names = [d["dish"] for d in dishes]
    assert names == ["隆江猪脚饭", "麻辣烫"]
    assert dishes[0]["source"] == "ai-city"
    assert dishes[0]["region"] == "商洛市"


def test_parse_returns_empty_on_garbage():
    assert city_dishes._parse("模型不想听话，直接说了一堆没有JSON的内容", "商洛市") == []
    assert city_dishes._parse("[]", "商洛市") == []


def test_fetch_uses_cache_and_silently_degrades(monkeypatch):
    city_dishes.reset_cache()
    good = _FakeClient(content='[{"name": "酸汤饺子", "taste": "salty", "staple": "noodle", "heaviness": "heavy"}]')
    monkeypatch.setattr(city_dishes, "_client", lambda settings=None: good)
    dishes = city_dishes.fetch_city_dishes("商洛市 · 商州区", _settings())
    assert [d["dish"] for d in dishes] == ["酸汤饺子"]
    assert good.calls == 1
    # 同城市第二次直接走缓存，不再调模型
    again = city_dishes.fetch_city_dishes("商洛市 · 商州区", _settings())
    assert again == dishes
    assert good.calls == 1
    city_dishes.reset_cache()


def test_fetch_swallows_model_errors(monkeypatch):
    city_dishes.reset_cache()
    broken = _FakeClient(error=RuntimeError("network down"))
    monkeypatch.setattr(city_dishes, "_client", lambda settings=None: broken)
    assert city_dishes.fetch_city_dishes("西安市", _settings()) == []
    city_dishes.reset_cache()


def test_fetch_skips_when_key_missing(monkeypatch):
    city_dishes.reset_cache()
    untouched = _FakeClient(content="[]")
    monkeypatch.setattr(city_dishes, "_client", lambda settings=None: untouched)
    bare = SimpleNamespace(
        openai_api_key="", openai_base_url="http://localhost:1/v1", openai_model="m",
        llm_price_per_1k_input=0.0, llm_price_per_1k_output=0.0,
    )
    assert city_dishes.fetch_city_dishes("商洛市", bare) == []
    assert untouched.calls == 0
    city_dishes.reset_cache()


@pytest.fixture()
def workers(monkeypatch):
    """记录真实线程并 join，测试不靠 sleep/轮询猜完成时间。"""
    real_thread = threading.Thread
    threads = []

    def tracked_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        threads.append(worker)
        return worker

    city_dishes.reset_cache()
    monkeypatch.setattr(city_dishes.threading, "Thread", tracked_thread)
    yield threads
    for worker in threads:
        worker.join(timeout=3)
        assert not worker.is_alive()
    assert not city_dishes._inflight
    city_dishes.reset_cache()


def test_prefetch_warms_cache_without_blocking(monkeypatch, workers):
    good = _FakeClient(content='[{"name": "砂锅土豆粉", "taste": "spicy", "staple": "noodle", "heaviness": "heavy"}]')
    monkeypatch.setattr(city_dishes, "_client", lambda settings=None: good)
    assert city_dishes.prefetch("商洛市 · 商州区", _settings(), allow_model=lambda: True)
    workers[0].join(timeout=3)
    assert [d["dish"] for d in city_dishes.cached("商洛市")] == ["砂锅土豆粉"]
    assert good.calls == 1
    assert not city_dishes.prefetch(
        "商洛市", _settings(), allow_model=lambda: pytest.fail("cache hit must not consume quota"),
    )
    assert len(workers) == 1


def test_prefetch_bounds_workers_and_deduplicates_city(monkeypatch, workers):
    gate = threading.Event()
    monkeypatch.setattr(city_dishes, "_MAX_PREFETCH_WORKERS", 2)

    def waiting_fetch(*args, **kwargs):
        assert gate.wait(timeout=3)
        return []

    monkeypatch.setattr(city_dishes, "fetch_city_dishes", waiting_fetch)
    admitted = []

    def admit():
        admitted.append(True)
        return True

    try:
        assert city_dishes.prefetch("北京市 · 朝阳区", _settings(), allow_model=admit)
        assert not city_dishes.prefetch("北京市 · 海淀区", _settings(), allow_model=admit)
        assert city_dishes.prefetch("上海市", _settings(), allow_model=admit)
        city_dishes.reset_cache()  # 清缓存也不能丢失在途容量
        assert not city_dishes.prefetch("杭州市", _settings(), allow_model=admit)
        assert len(workers) == len(admitted) == 2
    finally:
        gate.set()
        for worker in workers:
            worker.join(timeout=3)
    assert not city_dishes._inflight
    assert city_dishes.prefetch("杭州市", _settings(), allow_model=admit)


@pytest.mark.parametrize("stage", ["client", "fetch"])
def test_prefetch_releases_on_worker_exception(monkeypatch, workers, stage):
    def broken(*args, **kwargs):
        raise RuntimeError("offline test")

    monkeypatch.setattr(city_dishes, "_client" if stage == "client" else "fetch_city_dishes", broken)
    assert city_dishes.prefetch("北京市", _settings(), allow_model=lambda: True)
    workers[0].join(timeout=3)
    assert not city_dishes._inflight
    assert city_dishes.cached("北京市") == []
    # 同城可重试，不遗留 inflight 标记。
    assert city_dishes.prefetch("北京市", _settings(), allow_model=lambda: True)


@pytest.mark.parametrize("stage", ["denied", "admission_error", "thread_start"])
def test_prefetch_releases_when_admission_or_start_fails(monkeypatch, stage):
    def admit():
        if stage == "admission_error":
            raise RuntimeError("admission failed")
        return stage != "denied"

    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    monkeypatch.setattr(city_dishes.threading, "Thread", BrokenThread)
    assert not city_dishes.prefetch("北京市", _settings(), allow_model=admit)
    assert not city_dishes._inflight


def test_prefetch_without_key_does_not_consume_quota():
    settings = _settings()
    settings.openai_api_key = ""
    assert not city_dishes.prefetch(
        "北京市", settings, allow_model=lambda: pytest.fail("no key must not consume quota"),
    )
    assert not city_dishes._inflight
