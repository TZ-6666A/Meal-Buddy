from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import recipe_photos


@pytest.fixture(autouse=True)
def clear_photo_state():
    recipe_photos._search_cache.clear()
    recipe_photos._image_cache.clear()
    yield
    recipe_photos._search_cache.clear()
    recipe_photos._image_cache.clear()


def test_search_accepts_only_pexels_image_host(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "photos": [{
                    "src": {"large": "https://images.pexels.com/photos/1/photo.jpeg"},
                    "photographer": "Chef",
                    "photographer_url": "https://www.pexels.com/@chef/",
                    "url": "https://www.pexels.com/photo/1/",
                }]
            }

    class FakeClient:
        async def get(self, *args, **kwargs):
            assert kwargs["headers"]["Authorization"] == "test-key"
            return FakeResponse()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pexels_api_key="test-key"))

    import asyncio
    result = asyncio.run(recipe_photos._find_photo("番茄炒蛋"))

    assert result["url"] == "https://images.pexels.com/photos/1/photo.jpeg"
    assert result["photographer"] == "Chef"


def test_search_rejects_untrusted_image_host(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"photos": [{"src": {"large": "https://attacker.invalid/image.jpg"}}]}

    class FakeClient:
        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(recipe_photos, "_client_for_requests", lambda: FakeClient())
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pexels_api_key="test-key"))

    import asyncio
    assert asyncio.run(recipe_photos._find_photo("番茄炒蛋")) is None


def test_photo_endpoint_returns_named_placeholder_when_no_api_key(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pexels_api_key=""))
    response = client.get("/api/v1/recipe-photo", params={"name": "无油豆腐蒸蛋"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert "无油豆腐蒸蛋".encode() in response.content


def test_image_cache_respects_byte_budget(monkeypatch):
    recipe_photos._image_cache.clear()
    monkeypatch.setattr(recipe_photos, "MAX_IMAGE_CACHE_BYTES", 5)
    monkeypatch.setattr(recipe_photos, "MAX_CACHE_ENTRIES", 2)

    recipe_photos._cache_image("https://images.pexels.com/photos/1/a.jpg", b"1234", "image/jpeg")
    recipe_photos._cache_image("https://images.pexels.com/photos/2/b.jpg", b"6789", "image/jpeg")

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
    photo = {"url": "https://images.pexels.com/photos/1/photo.jpeg"}
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


async def _async_value(value):
    return value


def test_stream_recommendation_includes_photo_fields(client, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pexels_api_key=""))
    response = client.post("/api/v1/recommendations/stream", json={"available_minutes": 30})

    assert response.status_code == 200
    assert '"stage": "done"' in response.text
    assert '"image_url": "/api/v1/recipe-photo?' in response.text
    assert '"image_fallback_url": "/api/v1/recipe-photo?' in response.text


def test_recommendation_has_same_origin_photo_fields(client, fake_settings, monkeypatch):
    monkeypatch.setattr(recipe_photos, "get_settings", lambda: SimpleNamespace(pexels_api_key=""))
    response = client.post("/api/v1/recommendations", json={"available_minutes": 30})

    assert response.status_code == 200
    recipe = response.json()["recipes"][0]
    assert recipe["image_url"].startswith("/api/v1/recipe-photo?")
    assert recipe["image_fallback_url"].startswith("/api/v1/recipe-photo?")
    assert recipe["image_alt"]
    assert recipe["image_credit"] == ""
