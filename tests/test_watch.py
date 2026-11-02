import asyncio

import pytest

from app.services import watch


SEARCH_HTML = """
<html><body>
  <div class="bili-video-card">
    <div class="bili-video-card__wrap">
      <a href="//www.bilibili.com/video/BV1xx411c7mD/">
        <picture><source srcset="//i0.hdslb.com/bfs/archive/example.jpg@672w_378h_1c_!web-search-common-cover.avif 1x">
          <img src="//i0.hdslb.com/bfs/archive/example.jpg" alt="中华小当家">
        </picture>
      </a>
    </div>
    <h3 class="bili-video-card__info--tit" title="《&lt;em class=&quot;keyword&quot;&gt;中华&lt;/em&gt;小当家》">标题</h3>
    <a><span class="bili-video-card__info--author">美食UP</span></a>
    <span class="bili-video-card__stats--item"><svg></svg><span>1.2万</span></span>
    <span class="bili-video-card__stats--item"><svg></svg><span>345</span></span>
  </div>
</body></html>
"""


class FakeResponse:
    def __init__(self, text="", payload=None, status_code=200):
        self.text = text
        self.payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    html = ""
    pages = {}
    calls = []
    api_calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def get(self, url, *, params):
        self.calls.append((url, params))
        if url == watch.VIDEO_SEARCH_URL:
            return FakeResponse(self.html)
        self.api_calls.append((url, params))
        key = (params["keyword"], params.get("tids"), params["order"], params["page"])
        legacy_key = (params["keyword"], params["order"], params["page"])
        result = self.pages.get(key, self.pages.get(legacy_key, self.pages.get(
            params["page"], {"code": 0, "data": {"numResults": 1000, "result": []}}
        )))
        if isinstance(result, list):
            result = result.pop(0)
        return result if isinstance(result, FakeResponse) else FakeResponse(payload=result)

    async def aclose(self):
        return None


def patch_search(monkeypatch, html=SEARCH_HTML, pages=None):
    watch._search_page_cache.clear()
    watch._search_prefetch_keys.clear()
    watch._search_prefetch_tasks.clear()
    watch._search_client = None
    watch._search_client_loop = None
    watch._search_client_lock = None
    watch._search_client_warmed = False
    FakeClient.html = html
    FakeClient.pages = pages or {
        1: {
            "code": 0,
            "data": {
                "numResults": 1,
                "result": [{
                    "title": "《<em>中华</em>小当家》",
                    "pic": "//i0.hdslb.com/bfs/archive/example.jpg",
                    "bvid": "BV1xx411c7mD",
                    "author": "美食UP",
                    "play": "1.2万",
                    "typename": "动画",
                }],
            },
        }
    }
    FakeClient.calls = []
    FakeClient.api_calls = []
    monkeypatch.setattr(watch.httpx, "AsyncClient", FakeClient)


def test_watch_search_returns_bilibili_cards(client, monkeypatch):
    patch_search(monkeypatch)

    response = client.get("/api/v1/watch-search?q=小当家&category=动画&page=1&visit_id=test-visit")

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == [{
        "title": "《中华小当家》",
        "cover": "https://i0.hdslb.com/bfs/archive/example.jpg@672w_378h_1c_!web-search-common-cover.avif",
        "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
        "author": "美食UP",
        "play_count": 12000,
        "category": "动画",
    }]
    assert data["page"] == 1
    assert data["page_count"] == 50
    assert data["complete"] is False
    assert FakeClient.calls[0][0] == watch.VIDEO_SEARCH_API_URL
    assert FakeClient.api_calls[0][0] == watch.VIDEO_SEARCH_API_URL
    assert FakeClient.api_calls[0][1]["keyword"] == "小当家 动画"
    assert FakeClient.api_calls[0][1]["page"] == 1
    assert FakeClient.api_calls[0][1]["pagesize"] == 20
    assert FakeClient.api_calls[0][1]["order"] in watch.SEARCH_ORDERS


def test_watch_search_uses_default_food_query(client, monkeypatch):
    patch_search(monkeypatch, "<html><body>没有结果</body></html>")

    response = client.get("/api/v1/watch-search")

    assert response.status_code == 200
    assert FakeClient.calls[0][1]["keyword"] == "下饭"
    assert FakeClient.api_calls[0][1]["keyword"] == "下饭"
    assert response.json()["page_count"] == 150


def test_category_first_page_fills_sparse_ranking_results_concurrently(monkeypatch):
    pages = {}
    for order in watch.SEARCH_ORDERS:
        rows = [{
            "title": f"综艺 {order} {index}",
            "pic": f"//i0.hdslb.com/bfs/archive/{order}{index}.jpg",
            "bvid": f"BV{order[:2]}{index:08d}",
            "author": "美食UP",
            "play": "1000",
            "typename": "综艺",
        } for index in range(4)]
        pages[("综艺", "71", order, 1)] = {
            "code": 0,
            "data": {"numResults": 4, "result": rows},
        }
    patch_search(monkeypatch, "<html></html>", pages)

    result = asyncio.run(watch.search_videos("", "综艺", 1, "variety-first-page"))

    first_page_calls = [call for call in FakeClient.api_calls if call[1]["page"] == 1]
    assert len(result["items"]) == 8
    assert {call[1]["order"] for call in first_page_calls} == set(watch.SEARCH_ORDERS)
    assert all(call[1]["tids"] == "71" for call in first_page_calls)


@pytest.mark.parametrize(("category", "page", "page_count", "upstream_page"), [
    ("all", 1, 150, 1),
    ("all", 51, 150, 1),
    ("all", 150, 150, 50),
    ("动画", 1, 50, 1),
    ("动画", 50, 50, 50),
])
def test_watch_search_maps_ui_page_to_upstream_page(monkeypatch, category, page, page_count, upstream_page):
    rows = [{
        "title": f"第 {page} 页视频 {index}",
        "pic": f"//i0.hdslb.com/bfs/archive/BV{upstream_page:02d}{index:08d}.jpg",
        "bvid": f"BV{upstream_page:02d}{index:08d}",
        "author": "美食UP",
        "play": "1000",
        "typename": category,
    } for index in range(20)]
    patch_search(monkeypatch, "<html></html>", {
        upstream_page: {"code": 0, "data": {"numResults": 1000, "result": rows}}
    })

    result = asyncio.run(watch.search_videos("下饭", category, page, "visit-fixed"))

    assert len(result["items"]) == 8
    assert result["page"] == page
    assert result["page_count"] == page_count
    assert result["complete"] is True
    assert FakeClient.api_calls[0][1]["page"] == upstream_page


def test_watch_search_prefetches_next_page_for_instant_pagination(monkeypatch):
    rows_by_page = {
        page: [{
            "title": f"预取视频 {page}-{index}",
            "pic": f"//i0.hdslb.com/bfs/archive/BV{page:02d}{index:08d}.jpg",
            "bvid": f"BV{page:02d}{index:08d}",
            "author": "美食UP",
            "play": "1000",
        } for index in range(20)]
        for page in (1, 2)
    }
    pages = {
        page: {"code": 0, "data": {"numResults": 1000, "result": rows}}
        for page, rows in rows_by_page.items()
    }
    patch_search(monkeypatch, "<html></html>", pages)

    async def verify():
        first = await watch.search_videos("", "all", 1, "prefetch-visit")
        await asyncio.sleep(0.01)
        assert len(first["items"]) == 8
        assert [call[1]["page"] for call in FakeClient.api_calls] == [1, 2]

        second = await watch.search_videos("", "all", 2, "prefetch-visit")
        assert len(second["items"]) == 8
        assert [call[1]["page"] for call in FakeClient.api_calls] == [1, 2]

    asyncio.run(verify())


def test_watch_search_changes_results_for_a_new_visit(monkeypatch):
    rows = [{
        "title": f"推荐视频 {index}",
        "pic": f"//i0.hdslb.com/bfs/archive/BV01{index:08d}.jpg",
        "bvid": f"BV01{index:08d}",
        "author": "美食UP",
        "play": "1000",
    } for index in range(20)]
    patch_search(monkeypatch, "<html></html>", {
        page: {"code": 0, "data": {"numResults": 1000, "result": rows}}
        for page in range(1, 51)
    })

    first = asyncio.run(watch.search_videos("下饭", "all", 1, "visit-one"))
    repeated = asyncio.run(watch.search_videos("下饭", "all", 1, "visit-one"))
    next_visit = asyncio.run(watch.search_videos("下饭", "all", 1, "visit-two"))

    assert first["items"] == repeated["items"]
    assert first["items"] != next_visit["items"]


def test_watch_search_rejects_unknown_category(client):
    assert client.get("/api/v1/watch-search?category=invalid").status_code == 422


def test_watch_search_rejects_page_outside_requested_range(client):
    assert client.get("/api/v1/watch-search?page=151").status_code == 422


def test_watch_search_maps_upstream_failure(client, monkeypatch):
    async def fail(*_args):
        raise watch.WatchSearchUnavailable

    monkeypatch.setattr(watch, "search_videos", fail)

    response = client.get("/api/v1/watch-search")

    assert response.status_code == 502
    assert response.json()["detail"] == "哔哩哔哩搜索暂时不可用"


def test_video_item_rejects_untrusted_cover_host():
    item = watch._video_item({
        "title": "<em>测试</em>",
        "cover": "https://example.invalid/image.jpg",
        "bvid": "BV1xx411c7mD",
    }, "电影")

    assert item["title"] == "测试"
    assert item["cover"] == ""
    assert item["url"].startswith("https://www.bilibili.com/video/")


def test_cover_url_preserves_transformed_thumbnail_variant():
    assert watch._cover_url(
        "https://i0.hdslb.com/bfs/archive/example.jpg@672w_378h_1c_!web-search-common-cover.avif"
    ) == "https://i0.hdslb.com/bfs/archive/example.jpg@672w_378h_1c_!web-search-common-cover.avif"


def test_watch_cover_proxy_returns_image_bytes(client, monkeypatch):
    async def fake_fetch(url):
        assert url == "https://i0.hdslb.com/bfs/archive/example.jpg"
        return b"image-bytes", "image/jpeg"

    monkeypatch.setattr(watch, "fetch_cover", fake_fetch)
    response = client.get(
        "/api/v1/watch-cover",
        params={"url": "https://i0.hdslb.com/bfs/archive/example.jpg"},
    )

    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "public, max-age=86400, stale-while-revalidate=604800"


def test_fetch_cover_reuses_client_and_cached_bytes(monkeypatch):
    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg", "content-length": "5"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def aiter_bytes(self):
            yield b"cover"

    class FakeCoverClient:
        instances = 0
        requests = 0

        def __init__(self, **_kwargs):
            type(self).instances += 1

        def stream(self, *_args, **_kwargs):
            type(self).requests += 1
            return FakeStreamResponse()

        async def aclose(self):
            return None

    async def verify():
        await watch.close_cover_client()
        monkeypatch.setattr(watch.httpx, "AsyncClient", FakeCoverClient)
        url = "https://i0.hdslb.com/bfs/archive/example.jpg"
        assert await watch.fetch_cover(url) == (b"cover", "image/jpeg")
        assert await watch.fetch_cover(url) == (b"cover", "image/jpeg")
        await watch.close_cover_client()

    asyncio.run(verify())
    assert FakeCoverClient.instances == 1
    assert FakeCoverClient.requests == 1


def test_fetch_cover_falls_back_to_mirror_when_original_host_fails(monkeypatch):
    requested = []

    class FakeResponse:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"content-type": "image/jpeg", "content-length": "5"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def aiter_bytes(self):
            yield b"cover"

    class FakeCoverClient:
        def __init__(self, **_kwargs):
            pass

        def stream(self, _method, url, **_kwargs):
            requested.append(url)
            return FakeResponse(503 if "i1.hdslb.com" in url else 200)

        async def aclose(self):
            return None

    async def verify():
        await watch.close_cover_client()
        monkeypatch.setattr(watch.httpx, "AsyncClient", FakeCoverClient)
        url = "https://i1.hdslb.com/bfs/archive/example.jpg"
        assert await watch.fetch_cover(url) == (b"cover", "image/jpeg")
        await watch.close_cover_client()

    asyncio.run(verify())
    assert len(requested) == 2
    assert requested[0].startswith("https://i1.hdslb.com/")
    assert requested[1].startswith("https://i0.hdslb.com/")


def test_watch_cover_proxy_rejects_untrusted_url(client, monkeypatch):
    async def reject(_url):
        raise ValueError("invalid")

    monkeypatch.setattr(watch, "fetch_cover", reject)
    response = client.get(
        "/api/v1/watch-cover",
        params={"url": "https://example.invalid/image.jpg"},
    )

    assert response.status_code == 400


@pytest.mark.parametrize("url", [
    "http://i0.hdslb.com/bfs/archive/example.jpg",
    "https://example.invalid/bfs/archive/example.jpg",
    "https://i0.hdslb.com:8443/bfs/archive/example.jpg",
    "https://i0.hdslb.com/redirect/example.jpg",
])
def test_fetch_cover_rejects_non_bilibili_image_urls(url):
    with pytest.raises(ValueError, match="封面地址无效"):
        asyncio.run(watch.fetch_cover(url))


def test_play_count_parses_bilibili_units():
    assert watch._play_count("1.2万") == 12000
    assert watch._play_count("3亿播放") == 300_000_000
