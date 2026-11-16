from __future__ import annotations

import html
import re
import time
from collections import OrderedDict
from urllib.parse import quote, urlencode, urlsplit

import httpx

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RecipePhoto

PIXABAY_API = "https://pixabay.com/api/"
ALLOWED_IMAGE_HOSTS = {"cdn.pixabay.com", "pixabay.com"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/avif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SEARCH_TTL = 6 * 60 * 60
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
    ) and _image_cache:
        _, (_, removed, _) = _image_cache.popitem(last=False)
        _image_cache_bytes -= len(removed)


_FOOD_WORDS = {
    "鸡蛋": "egg", "蛋": "egg", "豆腐": "tofu", "番茄": "tomato", "西红柿": "tomato",
    "土豆": "potato", "茄子": "eggplant", "黄瓜": "cucumber", "鸡肉": "chicken",
    "鸡": "chicken", "牛肉": "beef", "猪肉": "pork", "虾": "shrimp", "虾仁": "shrimp",
    "鱼": "fish", "面": "noodles", "米饭": "rice", "白菜": "cabbage", "青椒": "pepper",
    "蘑菇": "mushroom", "玉米": "corn", "南瓜": "pumpkin", "菠菜": "spinach",
}
_DISH_WORDS = {
    "宫保鸡丁": "kung pao chicken", "宫爆鸡丁": "kung pao chicken", "麻婆豆腐": "mapo tofu",
    "红烧肉": ("braised pork belly", "pork belly"), "糖醋里脊": ("sweet and sour pork", "sweet sour pork"), "鱼香肉丝": "yu xiang pork",
    "排骨饭": "pork ribs", "可乐排骨饭": "pork ribs", "可乐排骨": "pork ribs",
    "披萨": "pizza", "披萨饭": "pizza", "黄焖鸡米饭": "chicken rice", "鸡米饭": "chicken rice",
    "猪脚饭": "pork trotters rice", "猪蹄饭": "pork trotters rice", "盐煎肉盖饭": "pork belly rice",
    "菠萝咕咾肉饭": "sweet and sour pork", "菠萝咕噜肉饭": "sweet and sour pork",
    "咕咾肉": "sweet and sour pork", "咕噜肉": "sweet and sour pork", "糖醋肉": "sweet and sour pork",
    "番茄炒蛋": "tomato egg", "西红柿炒鸡蛋": "tomato egg", "无油豆腐蒸蛋": "steamed tofu egg",
    "鳗鱼饭": ("eel rice", "unadon", "unagi don"), "鳗鱼": ("grilled eel", "unagi", "eel"),
    "红烧肉盖饭": ("pork belly rice", "braised pork rice", "roasted pork rice"), "红烧肉饭": ("pork belly rice", "braised pork rice"),
    "卤肉饭": ("braised pork rice", "pork rice", "lu rou fan"),
    "糖醋里脊盖饭": ("sweet and sour pork rice", "sweet sour pork rice"),
    "糖醋里脊饭": ("sweet and sour pork rice", "sweet sour pork rice"),
    "蜜汁叉烧饭": ("char siu rice", "bbq pork rice", "roast pork rice"), "叉烧饭": ("char siu rice", "bbq pork rice", "roast pork rice"),
    "叉烧": ("char siu", "bbq pork", "roast pork"),
    "牛肉饭": ("gyudon", "beef rice bowl", "beef rice"), "牛丼": ("gyudon", "beef rice bowl", "beef rice"),
    "咖喱鸡饭": ("chicken curry rice", "curry chicken"), "咖喱饭": ("curry rice", "japanese curry", "curry"),
    "蛋炒饭": ("egg fried rice", "fried rice"), "炒饭": ("fried rice",),
    "拉面": ("ramen",), "牛肉面": ("beef noodle soup", "beef noodles"), "乌冬面": ("udon",),
    "饺子": ("dumplings",), "煎饺": ("fried dumplings",), "蒸饺": ("steamed dumplings",),
    "小笼包": ("soup dumplings", "xiaolongbao"), "生煎": ("pan fried buns",),
    "包子": ("steamed buns",), "馒头": ("steamed buns",),
    "火锅": ("hot pot",), "麻辣烫": ("malatang", "spicy soup"), "麻辣香锅": ("spicy stir fry", "dry pot"),
    "寿司": ("sushi",), "饭团": ("onigiri", "rice ball"),
    "汉堡": ("burger",), "炸鸡": ("fried chicken",), "韩式炸鸡": ("korean fried chicken", "fried chicken"),
    "薯条": ("french fries",), "牛排": ("steak",), "烤肉": ("grilled meat", "barbecue"),
    "烧烤": ("barbecue", "bbq"), "意大利面": ("pasta", "spaghetti"), "意面": ("pasta", "spaghetti"),
    "沙拉": ("salad",),
    "酸菜鱼": ("sauerkraut fish", "fish soup"), "水煮鱼": ("boiled fish", "spicy fish"),
    "烤鱼": ("grilled fish",), "红烧鱼": ("braised fish",), "清蒸鱼": ("steamed fish",),
    "小龙虾": ("crayfish", "crawfish"), "螃蟹": ("crab dish", "crab"),
    "家常豆腐": ("home style tofu", "tofu dish"), "豆腐": ("tofu dish", "tofu"),
    "鸡蛋羹": ("steamed egg",), "蒸蛋": ("steamed egg",),
    "皮蛋瘦肉粥": ("congee", "rice porridge"), "粥": ("porridge", "congee"),
    "海南鸡饭": ("hainanese chicken rice", "chicken rice"),
    "猪排饭": ("pork cutlet rice", "tonkatsu"), "鸡排饭": ("chicken cutlet rice",),
    "煲仔饭": ("claypot rice",), "石锅拌饭": ("bibimbap",), "蛋包饭": ("omurice",),
    "三明治": ("sandwich",), "热狗": ("hot dog",), "墨西哥卷": ("taco", "burrito"),
    "天妇罗": ("tempura",), "寿喜锅": ("sukiyaki",), "关东煮": ("oden",), "泡菜": ("kimchi",),
    "烤鸭": ("roast duck", "peking duck"), "羊肉串": ("lamb skewers", "kebab"),
    "肉夹馍": ("chinese burger", "roujiamo"), "煎饼": ("pancake", "jianbing"),
    "螺蛳粉": ("luosifen", "rice noodles"), "米线": ("rice noodles",),
    "馄饨": ("wonton",), "云吞": ("wonton",),
    "炸酱面": ("zhajiangmian", "noodles"), "热干面": ("hot dry noodles", "noodles"),
    "酸辣粉": ("hot and sour noodles",), "盖饭": ("rice bowl",),
    # 商洛本地菜：没有对应菜名时容易退化成“米饭”搜到水稻田，这里给具体成品菜查询。
    "商洛洋芋糍粑": ("mashed potato", "potato cake"), "洋芋糍粑": ("mashed potato", "potato cake"),
    "商洛搅团": ("cornmeal porridge", "cornmeal"), "搅团": ("cornmeal porridge", "cornmeal"),
    "商洛浆水面": ("noodle soup", "cold noodles"), "浆水面": ("noodle soup", "cold noodles"),
    "洛南豆腐": ("tofu dish", "tofu"), "洛南核桃馍": ("walnut bread", "flatbread"),
    "核桃馍": ("walnut bread", "flatbread"),
    "镇安腊肉炒土豆": ("bacon potato", "bacon potato stir fry"), "腊肉炒土豆": ("bacon potato", "bacon potato stir fry"),
    "商州烩菜配米饭": ("vegetable stew", "stewed vegetables", "braised vegetables"),
    "烩菜配米饭": ("vegetable stew", "stewed vegetables", "braised vegetables"),
    "烩菜": ("vegetable stew", "stewed vegetables", "braised vegetables"),
    "商洛柿饼": ("dried persimmon", "persimmon"), "柿饼": ("dried persimmon", "persimmon"),
    "冒菜": ("malatang", "spicy soup"), "紫菜蛋花汤": ("egg soup", "seaweed soup"),
    "冬瓜排骨汤": ("pork rib soup", "winter melon soup"),
    "香菇滑鸡": ("chicken mushroom", "mushroom chicken"),
    "清蒸鲈鱼": ("steamed fish", "steamed sea bass"), "虾仁蒸蛋": ("steamed egg", "shrimp egg"),
}
_COOKING_WORDS = {
    "蒸": "steamed", "炒": "stir fry", "煎": "pan fried", "烤": "roasted", "汤": "soup",
    "粥": "porridge", "炖": "stew", "焖": "braised", "凉拌": "cold salad",
}

# 这些标签通常意味着“市场/摊位/生食材/风景/黑白片”，不是成品菜。
_BAD_TAGS = {
    "blackandwhite", "monochrome", "grayscale", "greyscale", "noir",
    "market", "stall", "vendor", "shop", "store", "supermarket", "fishmonger", "butcher",
    "raw", "uncooked", "fishing", "fisherman", "boat", "harbor", "harbour", "dock",
    "display", "counter", "crate", "ingredient", "ingredients",
    "field", "paddy", "harvest", "farm", "farmer", "animal", "wildlife", "underwater",
    "sea", "ocean", "lake", "river", "landscape",
    "live", "kitchen", "chef", "menu",
}
_COOKED_TAGS = {
    "food", "dish", "meal", "cuisine", "plate", "bowl", "cooked", "grilled", "roasted",
    "fried", "baked", "steamed", "soup", "rice", "noodles", "pasta", "curry", "barbecue",
    "bbq", "restaurant", "dinner", "lunch", "breakfast",
}
SEARCH_CACHE_VERSION = "pixabay-v2"


def _normalized_query(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()[:80]


def _dish_queries(query: str) -> list[str]:
    """菜品名可能有包含关系，优先采用最长的匹配项，例如“红烧肉盖饭”优先于“红烧肉”。"""
    matches = [token for token in _DISH_WORDS if token in query]
    if not matches:
        return []
    value = _DISH_WORDS[max(matches, key=len)]
    if isinstance(value, str):
        return [value]
    return [item for item in value if item]


def _query_variants(name: str) -> list[str]:
    query = _normalized_query(name)
    if not query:
        return []
    dishes = _dish_queries(query)
    if dishes:
        return list(dict.fromkeys(dishes))
    food = next((english for token, english in _FOOD_WORDS.items() if token in query), "")
    method = next((english for token, english in _COOKING_WORDS.items() if token in query), "")
    # 只有“食材 + 做法”才继续搜索；单纯一个 “fish/chicken/rice”
    # 容易搜到市场、活鱼、农田，宁可保持无图。
    if not (food and method):
        return []
    variants = [f"{food} {method}"]
    return list(dict.fromkeys(variant.strip() for variant in variants if variant.strip()))


def _safe_pixabay_page_url(value: object) -> str:
    url = str(value or "")
    parsed = urlsplit(url)
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"pixabay.com", "www.pixabay.com"}
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and parsed.path.startswith(("/photos/", "/get/"))
    ):
        return url
    return "https://pixabay.com/"


def _safe_pixabay_image_url(value: object) -> str:
    url = str(value or "")
    parsed = urlsplit(url)
    if (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_IMAGE_HOSTS
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and parsed.path.startswith(("/photo/", "/get/"))
    ):
        return url
    return ""


def _photo_from_payload(photo: object) -> dict | None:
    if not isinstance(photo, dict):
        return None
    image_url = _safe_pixabay_image_url(photo.get("largeImageURL") or photo.get("webformatURL"))
    if not image_url:
        return None
    user = str(photo.get("user") or "Pixabay")[:100]
    user_id = str(photo.get("user_id") or "").strip()
    photographer_url = (
        f"https://pixabay.com/users/{quote(user, safe='')}-{quote(user_id, safe='')}/"
        if user_id.isdigit() else "https://pixabay.com/"
    )
    return {
        "url": image_url,
        "photographer": user,
        "photographer_url": photographer_url,
        "pixabay_url": _safe_pixabay_page_url(photo.get("pageURL")),
        "tags": str(photo.get("tags") or ""),
    }


def _stem(token: str) -> str:
    """Pixabay 标签常用复数，做一点轻量归一化：vegetables -> vegetable。"""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("oes"):
        return token[:-2]
    if len(token) > 4 and token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _photo_tags(photo: dict) -> set[str]:
    return {
        _stem(token)
        for token in re.findall(r"[a-z]+", str(photo.get("tags") or "").lower())
    }


_STOPWORDS = {"and", "with", "the", "of", "in", "on", "a", "an", "to"}


def _query_tokens(text: str) -> set[str]:
    tokens = set()
    for token in re.findall(r"[a-z]+", text.lower()):
        if len(token) > 2 and token not in _STOPWORDS:
            tokens.add(_stem(token))
    return tokens


def _has_bad_tag(photo: dict) -> bool:
    text = str(photo.get("tags") or "").lower()
    if "black and white" in text or "black-and-white" in text or "blackandwhite" in text:
        return True
    return bool(_photo_tags(photo) & _BAD_TAGS)


def _candidate_score(query: str, photo: dict) -> int:
    tokens = _query_tokens(query)
    tags = _photo_tags(photo)
    # 匹配上菜名是基础分；带 food/dish/meal/plate 等成品菜标签的优先，
    # 但标签数量不能无限压过更精确的菜名，所以最多加 3 分。
    return len(tokens & tags) * 2 + min(len(tags & _COOKED_TAGS), 3)


def _matches_query(query: str, photo: dict) -> bool:
    tokens = _query_tokens(query)
    tags = _photo_tags(photo)
    return bool(tokens) and tokens.issubset(tags) and not _has_bad_tag(photo)


def _required_score(query: str) -> int:
    tokens = _query_tokens(query)
    return 2 if len(tokens) == 1 else len(tokens) * 2


async def _find_photo(name: str) -> dict | None:
    query = _normalized_query(name)
    variants = _query_variants(query)
    if not variants:
        return None
    cache_key = f"{SEARCH_CACHE_VERSION}|" + "|".join(variants)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return cached[0]
    key = str(getattr(get_settings(), "pixabay_api_key", "") or "").strip()
    if not key:
        _cache_set(_search_cache, cache_key, (None,), 300)
        return None
    try:
        candidates: list[dict] = []
        for rank, search_query in enumerate(variants):
            params = {
                "key": key,
                "q": search_query,
                "image_type": "photo",
                "per_page": 20,
                "safesearch": "true",
                "lang": "en",
                "category": "food",
                "order": "popular",
            }
            response = await _client_for_requests().get(PIXABAY_API, params=params)
            response.raise_for_status()
            for item in response.json().get("hits", []):
                photo = _photo_from_payload(item)
                if photo and _matches_query(search_query, photo):
                    # 越靠前的菜名变体越精确，同分时优先前面的。
                    photo["score"] = _candidate_score(search_query, photo) - rank
                    photo["query"] = search_query
                    candidates.append(photo)
        best = max(candidates, key=lambda photo: photo.get("score", 0), default=None)
        if best is None or best.get("score", 0) < _required_score(best.get("query", variants[0])):
            best = None
        _cache_set(_search_cache, cache_key, (best,), SEARCH_TTL)
        return best
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


def _safe_catalog_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _empty_photo_info() -> dict:
    return {
        "found": False,
        "image_url": "",
        "image_alt": "",
        "photographer": "",
        "photographer_url": "",
        "credit_name": "",
        "credit_url": "",
    }


async def photo_info(db: Session, name: str) -> dict:
    normalized_name = _normalized_query(name)
    photo = db.scalar(
        select(RecipePhoto).where(RecipePhoto.recipe_name == normalized_name)
    )
    if photo is not None and _safe_catalog_url(photo.image_url):
        return {
            "found": True,
            "image_url": photo.image_url,
            "image_alt": photo.image_alt or f"{photo.recipe_name}成品图片",
            "photographer": photo.photographer,
            "photographer_url": photo.credit_url if _safe_catalog_url(photo.credit_url) else "",
            "credit_name": photo.credit_name,
            "credit_url": photo.credit_url if _safe_catalog_url(photo.credit_url) else "",
        }

    pixabay_photo = await _find_photo(normalized_name)
    if pixabay_photo is None:
        return _empty_photo_info()
    return {
        "found": True,
        "image_url": f"/api/v1/recipe-photo?{urlencode({'name': normalized_name})}",
        "image_alt": f"{normalized_name}成品图片",
        "photographer": pixabay_photo["photographer"],
        "photographer_url": pixabay_photo["photographer_url"],
        "credit_name": f"Photo by {pixabay_photo['photographer']} on Pixabay",
        "credit_url": pixabay_photo["pixabay_url"],
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
    if not _safe_pixabay_image_url(url):
        return _placeholder(name), "image/svg+xml", False
    try:
        async with _client_for_requests().stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise ValueError("upstream status")
            if not _safe_pixabay_image_url(str(getattr(response, "url", url))):
                raise ValueError("untrusted image redirect")
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
