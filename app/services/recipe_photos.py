from __future__ import annotations

import html
import re
import time
from collections import OrderedDict
from urllib.parse import quote, urlencode, urlsplit

import httpx

from app.config import get_settings

PIXABAY_API = "https://pixabay.com/api/"
ALLOWED_IMAGE_HOST = "cdn.pixabay.com"
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/avif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Pixabay asks API consumers to cache search responses for 24 hours.
SEARCH_TTL = 24 * 60 * 60
IMAGE_TTL = 24 * 60 * 60
MAX_CACHE_ENTRIES = 256
MAX_IMAGE_CACHE_BYTES = 48 * 1024 * 1024

_search_cache: OrderedDict[str, tuple[float, dict | None]] = OrderedDict()
_image_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_image_cache_bytes = 0
_client: httpx.AsyncClient | None = None


def _client_for_requests() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(4.0, connect=1.5),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )
    return _client


async def close_client() -> None:
    global _client, _image_cache_bytes
    if _client is not None:
        await _client.aclose()
        _client = None
    _search_cache.clear()
    _image_cache.clear()
    _image_cache_bytes = 0


def _cache_get(cache: OrderedDict, key: str):
    entry = cache.get(key)
    if entry is None:
        return None
    if entry[0] <= time.monotonic():
        del cache[key]
        return None
    cache.move_to_end(key)
    return entry[1:]


def _cache_set(cache: OrderedDict, key: str, value: tuple, ttl: int) -> None:
    cache[key] = (time.monotonic() + ttl, *value)
    cache.move_to_end(key)
    while len(cache) > MAX_CACHE_ENTRIES:
        cache.popitem(last=False)


def _cached_image(url: str) -> tuple[bytes, str] | None:
    global _image_cache_bytes
    entry = _image_cache.get(url)
    if entry is None:
        return None
    expires_at, content, content_type = entry
    if expires_at <= time.monotonic():
        _image_cache_bytes -= len(content)
        del _image_cache[url]
        return None
    _image_cache.move_to_end(url)
    return content, content_type


def _cache_image(url: str, content: bytes, content_type: str) -> None:
    global _image_cache_bytes
    previous = _image_cache.pop(url, None)
    if previous:
        _image_cache_bytes -= len(previous[1])
    if len(content) > MAX_IMAGE_CACHE_BYTES:
        return
    _image_cache[url] = (time.monotonic() + IMAGE_TTL, content, content_type)
    _image_cache_bytes += len(content)
    while (
        _image_cache_bytes > MAX_IMAGE_CACHE_BYTES
        or len(_image_cache) > MAX_CACHE_ENTRIES
    ):
        _, (_, removed, _) = _image_cache.popitem(last=False)
        _image_cache_bytes -= len(removed)


_FOOD_WORDS = {
    "鸡蛋": "egg", "蛋": "egg", "豆腐": "tofu", "番茄": "tomato", "西红柿": "tomato",
    "土豆": "potato", "茄子": "eggplant", "黄瓜": "cucumber", "鸡肉": "chicken",
    "鸡": "chicken", "牛肉": "beef", "猪肉": "pork", "虾": "shrimp", "虾仁": "shrimp",
    "鱼": "fish", "面": "noodles", "米饭": "rice", "白菜": "cabbage", "青椒": "pepper",
    "蘑菇": "mushroom", "玉米": "corn", "南瓜": "pumpkin", "菠菜": "spinach",
}
_COOKING_WORDS = {
    "蒸": "steamed", "炒": "stir fry", "煎": "pan fried", "烤": "roasted", "汤": "soup",
    "粥": "porridge", "炖": "stew", "焖": "braised", "凉拌": "cold salad",
}


def _normalized_query(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()[:80]


def _query_variants(name: str) -> list[str]:
    query = _normalized_query(name)
    if not query:
        return []
    food = next((english for token, english in _FOOD_WORDS.items() if token in query), "")
    method = next((english for token, english in _COOKING_WORDS.items() if token in query), "dish")
    variants = [f"{query} food", f"{food} {method} food" if food else f"{method} food"]
    return list(dict.fromkeys(variant.strip() for variant in variants if variant.strip()))


def _safe_pixabay_url(value: object) -> str:
    url = str(value or "")
    parsed = urlsplit(url)
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"pixabay.com", "www.pixabay.com"}
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    ):
        return url
    return "https://pixabay.com/"


def _photo_from_payload(photo: object) -> dict | None:
    if not isinstance(photo, dict):
        return None
    image_url = str(photo.get("largeImageURL") or photo.get("webformatURL") or "")
    parsed = urlsplit(image_url)
    if (
        parsed.scheme != "https" or parsed.hostname != ALLOWED_IMAGE_HOST or parsed.port not in (None, 443)
        or parsed.username is not None or parsed.password is not None or parsed.fragment
        or not parsed.path.startswith("/photo/")
    ):
        return None
    user = str(photo.get("user") or "Pixabay")[:100]
    user_id = str(photo.get("user_id") or "").strip()
    photographer_url = (
        f"https://pixabay.com/users/{quote(user, safe='')}-{quote(user_id, safe='')}/"
        if user_id and re.fullmatch(r"\d+", user_id)
        else "https://pixabay.com/"
    )
    return {
        "url": image_url,
        "photographer": user,
        "photographer_url": photographer_url,
        "pixabay_url": _safe_pixabay_url(photo.get("pageURL")),
    }


async def _find_photo(name: str) -> dict | None:
    query = _normalized_query(name)
    variants = _query_variants(query)
    if not variants:
        return None
    cache_key = "pixabay|" + "|".join(variants)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return cached[0]
    key = str(getattr(get_settings(), "pixabay_api_key", "") or "").strip()
    if not key:
        _cache_set(_search_cache, cache_key, (None,), 300)
        return None
    try:
        for search_query in variants:
            response = await _client_for_requests().get(
                PIXABAY_API,
                params={
                    "key": key,
                    "q": search_query,
                    "image_type": "photo",
                    "per_page": 3,
                    "safesearch": "true",
                    "lang": "en",
                },
            )
            response.raise_for_status()
            hits = response.json().get("hits", [])
            photo = next(
                (candidate for hit in hits if (candidate := _photo_from_payload(hit))),
                None,
            )
            if photo:
                _cache_set(_search_cache, cache_key, (photo,), SEARCH_TTL)
                return photo
        _cache_set(_search_cache, cache_key, (None,), SEARCH_TTL)
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        _cache_set(_search_cache, cache_key, (None,), 60)
        return None


def _placeholder(name: str) -> bytes:
    title = html.escape(_normalized_query(name) or "美味家常菜")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 600">'
        '<rect width="960" height="600" fill="#edf2e9"/>'
        '<circle cx="480" cy="274" r="148" fill="#fff" stroke="#c7d8c7" stroke-width="14"/>'
        '<circle cx="480" cy="274" r="112" fill="#f4e8c9"/>'
        '<path d="M401 282c34-78 125-78 158 0-33 66-125 66-158 0Z" fill="#91ad79"/>'
        '<circle cx="444" cy="249" r="13" fill="#d87951"/><circle cx="511" cy="302" r="12" fill="#d87951"/>'
        f'<text x="480" y="500" text-anchor="middle" font-size="34" font-family="sans-serif" fill="#46614e">{title}</text>'
        '</svg>'
    )
    return svg.encode("utf-8")


def decorate_recipes(recipes: list) -> None:
    for recipe in recipes:
        recipe.image_url = f"/api/v1/recipe-photo?{urlencode({'name': recipe.name})}"
        recipe.image_fallback_url = f"/api/v1/recipe-photo?{urlencode({'name': recipe.name, 'fallback': 'true'})}"
        recipe.image_alt = f"{recipe.name}成品图片"
        recipe.image_photographer = ""
        recipe.image_credit = ""
        recipe.image_credit_url = ""


async def photo_info(name: str) -> dict:
    photo = await _find_photo(name)
    return {
        "found": photo is not None,
        "image_url": f"/api/v1/recipe-photo?{urlencode({'name': name})}",
        "photographer": photo["photographer"] if photo else "",
        "photographer_url": photo["photographer_url"] if photo else "",
        "pixabay_url": photo["pixabay_url"] if photo else "",
        "source_name": "Pixabay" if photo else "",
        "source_url": photo["pixabay_url"] if photo else "",
    }


async def fetch_photo(name: str, fallback_only: bool = False) -> tuple[bytes, str, bool]:
    if fallback_only:
        return _placeholder(name), "image/svg+xml", False
    photo = await _find_photo(name)
    if not photo:
        return _placeholder(name), "image/svg+xml", False
    url = photo["url"]
    cached = _cached_image(url)
    if cached is not None:
        return cached[0], cached[1], True
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_IMAGE_HOST or parsed.port not in (None, 443):
        return _placeholder(name), "image/svg+xml", False
    try:
        async with _client_for_requests().stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError("upstream status")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in ALLOWED_IMAGE_TYPES:
                raise ValueError("upstream content type")
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("image too large")
                chunks.append(chunk)
        content = b"".join(chunks)
        _cache_image(url, content, content_type)
        return content, content_type, True
    except (httpx.HTTPError, ValueError):
        return _placeholder(name), "image/svg+xml", False
