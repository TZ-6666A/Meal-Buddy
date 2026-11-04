"""AI 城市日常菜层：解析校验、缓存与静默降级行为。"""
from types import SimpleNamespace

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


def test_prefetch_warms_cache_without_blocking(monkeypatch):
    """prefetch 在后台线程预热缓存；cached 只读、未预热返回空。"""
    import time as _time

    city_dishes.reset_cache()
    assert city_dishes.cached("商洛市") == []  # 未预热：只读为空

    good = _FakeClient(content='[{"name": "砂锅土豆粉", "taste": "spicy", "staple": "noodle", "heaviness": "heavy"}]')
    monkeypatch.setattr(city_dishes, "_client", lambda settings=None: good)
    city_dishes.prefetch("商洛市 · 商州区")
    # 轮询等待后台线程完成（最多2秒）
    deadline = _time.monotonic() + 2
    while not city_dishes.cached("商洛市") and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert [d["dish"] for d in city_dishes.cached("商洛市 · 商州区")] == ["砂锅土豆粉"]
    assert good.calls == 1

    # 缓存保鲜期内重复 prefetch 不再调模型
    city_dishes.prefetch("商洛市")
    _time.sleep(0.2)
    assert good.calls == 1
    city_dishes.reset_cache()


def test_prefetch_never_raises_on_model_failure(monkeypatch):
    """后台预热失败只影响缓存，不向调用方抛异常。"""
    import time as _time

    city_dishes.reset_cache()
    broken = _FakeClient(error=RuntimeError("network down"))
    monkeypatch.setattr(city_dishes, "_client", lambda settings=None: broken)
    city_dishes.prefetch("北京市")  # 不应向调用方抛异常
    _time.sleep(0.3)  # 等后台线程走完失败路径
    assert city_dishes.cached("北京市") == []
    city_dishes.reset_cache()
