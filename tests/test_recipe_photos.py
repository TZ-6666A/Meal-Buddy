from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.models import RecipePhoto
from app.services import recipe_photos


@pytest.fixture(autouse=True)
def clear_photo_state():
    recipe_photos._search_cache.clear()
    recipe_photos._image_cache.clear()
    recipe_photos._image_cache_bytes = 0
    yield
    recipe_photos._search_cache.clear()
    recipe_photos._image_cache.clear()
    recipe_photos._image_cache_bytes = 0


def test_search_accepts_pixabay_image_and_credit(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "hits": [{
                    "largeImageURL": "https://pixabay.com/get/tomato.jpg",
                    "user": "Chef",
                    "user_id": 12345,
                    "pageURL": "https://pixabay.com/photos/tomato-food-1/",
                    "tags": "tomato egg food stir fry",
                }]
            }

    class FakeClient:
        async def get(self, *args, **kwargs):
            assert kwargs["params"]["key"] == "test-key"
            assert kwargs["params"]["image_type"] == "photo"
            return FakeResponse()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key="test-key"))

    import asyncio
    result = asyncio.run(recipe_photos._find_photo("番茄炒蛋"))

    assert result["url"] == "https://pixabay.com/get/tomato.jpg"
    assert result["photographer"] == "Chef"
    assert result["photographer_url"] == "https://pixabay.com/users/Chef-12345/"
    assert result["pixabay_url"] == "https://pixabay.com/photos/tomato-food-1/"


def test_search_rejects_untrusted_image_host(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": [{
                "largeImageURL": "https://attacker.invalid/image.jpg",
                "tags": "tomato egg food",
            }]}

    class FakeClient:
        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key="test-key"))

    import asyncio
    assert asyncio.run(recipe_photos._find_photo("番茄炒蛋")) is None


def test_photo_endpoint_returns_named_placeholder_when_catalog_misses(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key=""))
    response = client.get("/api/v1/recipe-photo", params={"name": "无油豆腐蒸蛋"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert "无油豆腐蒸蛋".encode() in response.content


def test_photo_info_reads_oss_url_and_credit_from_catalog(client, monkeypatch):
    with client.testing_session() as db:
        db.add(RecipePhoto(
            recipe_name="番茄炒蛋",
            image_url="https://fandazi.oss-cn-shanghai.aliyuncs.com/dishes/tomato-egg.webp",
            image_alt="番茄炒蛋成品",
            photographer="小饭",
            credit_name="饭搭子菜图",
            credit_url="https://fandazi.oss-cn-shanghai.aliyuncs.com/credits/terms",
        ))
        db.commit()

    response = client.get("/api/v1/recipe-photo-info", params={"name": "番茄炒蛋"})

    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "image_url": "https://fandazi.oss-cn-shanghai.aliyuncs.com/dishes/tomato-egg.webp",
        "image_alt": "番茄炒蛋成品",
        "photographer": "小饭",
        "photographer_url": "https://fandazi.oss-cn-shanghai.aliyuncs.com/credits/terms",
        "credit_name": "饭搭子菜图",
        "credit_url": "https://fandazi.oss-cn-shanghai.aliyuncs.com/credits/terms",
    }


def test_photo_info_returns_found_false_when_catalog_misses(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key=""))
    response = client.get("/api/v1/recipe-photo-info", params={"name": "豆腐蒸蛋"})

    assert response.status_code == 200
    assert response.json()["found"] is False
    assert response.json()["image_url"] == ""


def test_photo_info_prefers_catalog_over_pixabay(client, monkeypatch):
    with client.testing_session() as db:
        db.add(RecipePhoto(
            recipe_name="番茄炒蛋",
            image_url="https://fandazi.oss-cn-shanghai.aliyuncs.com/dishes/tomato-egg.webp",
            image_alt="目录图片",
            credit_name="目录署名",
            credit_url="https://fandazi.oss-cn-shanghai.aliyuncs.com/credits/terms",
        ))
        db.commit()

    async def should_not_search(name):
        raise AssertionError("catalog hits must not call Pixabay")

    monkeypatch.setattr(recipe_photos, "_find_photo", should_not_search)
    response = client.get("/api/v1/recipe-photo-info", params={"name": "番茄炒蛋"})

    assert response.status_code == 200
    assert response.json()["image_url"].endswith("tomato-egg.webp")
    assert response.json()["credit_name"] == "目录署名"


def test_photo_info_falls_back_to_pixabay_when_catalog_misses(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "_find_photo", lambda name: _async_value({
        "url": "https://pixabay.com/get/tofu.jpg",
        "photographer": "Chef",
        "photographer_url": "https://pixabay.com/users/Chef-1/",
        "pixabay_url": "https://pixabay.com/photos/tofu-food-1/",
    }))

    response = client.get("/api/v1/recipe-photo-info", params={"name": "豆腐蒸蛋"})

    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "image_url": f"/api/v1/recipe-photo?name={quote('豆腐蒸蛋')}",
        "image_alt": "豆腐蒸蛋成品图片",
        "photographer": "Chef",
        "photographer_url": "https://pixabay.com/users/Chef-1/",
        "credit_name": "Photo by Chef on Pixabay",
        "credit_url": "https://pixabay.com/photos/tofu-food-1/",
    }


def test_photo_info_rejects_non_https_catalog_url(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key=""))
    with client.testing_session() as db:
        db.add(RecipePhoto(recipe_name="番茄炒蛋", image_url="http://example.test/dish.jpg"))
        db.commit()

    response = client.get("/api/v1/recipe-photo-info", params={"name": "番茄炒蛋"})

    assert response.status_code == 200
    assert response.json()["found"] is False


def test_image_cache_respects_byte_budget(monkeypatch):
    recipe_photos._image_cache.clear()
    monkeypatch.setattr(recipe_photos, "MAX_IMAGE_CACHE_BYTES", 5)
    monkeypatch.setattr(recipe_photos, "MAX_CACHE_ENTRIES", 2)

    recipe_photos._cache_image("https://pixabay.com/get/1.jpg", b"1234", "image/jpeg")
    recipe_photos._cache_image("https://pixabay.com/get/2.jpg", b"6789", "image/jpeg")

    assert recipe_photos._image_cache_bytes <= 5
    assert len(recipe_photos._image_cache) == 1


def test_photo_endpoint_serves_only_local_fallback_when_requested(client, monkeypatch):
    async def no_photo(name, fallback_only=False):
        assert fallback_only is True
        return recipe_photos._placeholder(name), "image/svg+xml", False

    monkeypatch.setattr(recipe_photos, "fetch_photo", no_photo)
    response = client.get("/api/v1/recipe-photo", params={"name": "豆腐蒸蛋", "fallback": "true"})

    assert response.status_code == 200
    assert response.headers["x-recipe-photo"] == "placeholder"


def test_fetch_photo_rejects_non_image_content_type(monkeypatch):
    photo = {"url": "https://pixabay.com/get/photo.jpeg"}
    monkeypatch.setattr(recipe_photos, "_find_photo", lambda name: _async_value(photo))

    class FakeStream:
        status_code = 200
        headers = {"content-type": "text/html"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def aiter_bytes(self):
            yield b"not an image"

    class FakeClient:
        def stream(self, *args, **kwargs):
            return FakeStream()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    import asyncio
    content, content_type, found = asyncio.run(recipe_photos.fetch_photo("豆腐蒸蛋"))

    assert not found
    assert content_type == "image/svg+xml"
    assert "豆腐蒸蛋".encode() in content


def test_dish_mapping_prefers_specific_longest_name():
    assert recipe_photos._query_variants("鳗鱼饭") == ["eel rice", "unadon", "unagi don"]
    assert recipe_photos._query_variants("红烧肉盖饭") == [
        "pork belly rice", "braised pork rice", "roasted pork rice",
    ]
    assert recipe_photos._query_variants("红烧肉") == ["braised pork belly", "pork belly"]
    # 本地菜不能再退化成“米饭”去搜水稻田。
    assert recipe_photos._query_variants("商州烩菜配米饭") == [
        "vegetable stew", "stewed vegetables", "braised vegetables",
    ]
    assert recipe_photos._query_variants("商洛洋芋糍粑") == ["mashed potato", "potato cake"]
    # Pixabay 标签常是复数，vegetables 应能匹配 vegetable。
    assert recipe_photos._matches_query("vegetable stew", {"tags": "vegetables, stew, food"})
    # 只有食材、没有做法的泛词不再搜索，避免 fish 搜到鱼市场。
    assert recipe_photos._query_variants("鱼") == []
    assert recipe_photos._query_variants("米饭") == []


def test_search_rejects_market_and_black_and_white(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": [{
                "largeImageURL": "https://pixabay.com/get/market.jpg",
                "tags": "grilled fish market black and white",
            }]}

    class FakeClient:
        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key="test-key"))

    import asyncio
    assert asyncio.run(recipe_photos._find_photo("烤鱼")) is None


def test_search_accepts_cooked_food(monkeypatch):
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": [{
                "largeImageURL": "https://pixabay.com/get/fish.jpg",
                "user": "Chef",
                "user_id": 7,
                "pageURL": "https://pixabay.com/photos/fish-dish-7/",
                "tags": "grilled fish food plate",
            }]}

    class FakeClient:
        async def get(self, *args, **kwargs):
            seen.update(kwargs["params"])
            return FakeResponse()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key="test-key"))

    import asyncio
    result = asyncio.run(recipe_photos._find_photo("烤鱼"))

    assert result["url"] == "https://pixabay.com/get/fish.jpg"
    assert seen["q"] == "grilled fish"
    assert seen["category"] == "food"


async def _async_value(value):
    return value


def test_stream_recommendation_includes_photo_fields(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key=""))
    response = client.post("/api/v1/recommendations/stream", json={"available_minutes": 30})

    assert response.status_code == 200
    assert '"stage": "done"' in response.text
    assert '"image_url": "/api/v1/recipe-photo?' in response.text
    assert '"image_fallback_url": "/api/v1/recipe-photo?' in response.text


def test_recommendation_has_same_origin_photo_fields(client, fake_settings, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pixabay_api_key=""))
    response = client.post("/api/v1/recommendations", json={"available_minutes": 30})

    assert response.status_code == 200
    recipe = response.json()["recipes"][0]
    assert recipe["image_url"].startswith("/api/v1/recipe-photo?")
    assert recipe["image_fallback_url"].startswith("/api/v1/recipe-photo?")
    assert recipe["image_alt"]
    assert recipe["image_credit"] == ""
