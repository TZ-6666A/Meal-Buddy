from __future__ import annotations

import asyncio
import html
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
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
_search_tasks: dict[str, asyncio.Task] = {}
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
    # 家常小炒：泛化搜索容易落到食材/菜市场，这里都给成品菜查询。
    "青椒肉丝": ("pepper pork stir fry", "shredded pork"), "蒜蓉西兰花": ("broccoli dish", "broccoli garlic"),
    "手撕包菜": ("cabbage dish", "stir fried cabbage"), "清炒油麦菜": ("leafy vegetable", "stir fried greens"),
    "香菇青菜": ("mushroom vegetable", "bok choy mushroom"), "凉拌黄瓜": ("cucumber salad", "cucumber dish"),
    "蒜香虾仁": ("garlic shrimp", "shrimp dish"), "土豆炖鸡": ("chicken stew", "chicken potato stew"),
    "番茄牛腩": ("beef stew", "beef tomato stew"), "土豆炖牛肉": ("beef stew", "beef potato stew"),
    "小鸡炖蘑菇": ("chicken stew", "chicken mushroom stew"),
    "酸辣土豆丝": ("shredded potato", "potato dish"), "醋溜白菜": ("cabbage dish", "stir fried cabbage"),
    "蚝油生菜": ("lettuce dish", "lettuce salad"), "清炒菠菜": ("spinach dish", "spinach"),
    "蒜蓉空心菜": ("water spinach", "vegetable dish"), "韭菜炒鸡蛋": ("scrambled eggs", "chive"),
    "木耳炒鸡蛋": ("scrambled eggs", "wood ear"), "洋葱炒鸡蛋": ("scrambled eggs", "onion"),
    "蒜苗炒肉": ("pork dish", "stir fried pork"), "芹菜炒香干": ("celery dish", "tofu celery"),
    "小葱拌豆腐": ("tofu salad", "cold tofu"), "凉拌木耳": ("wood ear salad", "mushroom salad"),
    "皮蛋豆腐": ("tofu salad", "century egg"),
    "白灼虾": ("boiled shrimp", "shrimp dish"), "清炒虾仁": ("shrimp dish", "sauteed shrimp"),
    "番茄鸡蛋汤": ("tomato egg soup", "egg soup"), "丝瓜蛋汤": ("egg soup", "vegetable soup"),
    "菠菜蛋花汤": ("egg soup", "spinach soup"),
    "番茄烧豆腐": ("tomato tofu", "tofu dish"), "蒜苔炒肉": ("pork dish", "stir fried pork"),
    "木须肉": ("pork dish", "stir fried pork"), "京酱肉丝": ("shredded pork", "pork dish"),
    "蚂蚁上树": ("glass noodles", "vermicelli"), "干煸豆角": ("green beans", "string beans"),
    "干锅花菜": ("cauliflower dish", "cauliflower"), "地三鲜": ("eggplant dish", "braised eggplant"),
    "鱼香茄子": ("eggplant dish", "braised eggplant"), "回锅肉": ("pork belly", "braised pork"),
    "可乐鸡翅": ("chicken wings", "braised chicken"), "红烧鸡翅": ("chicken wings", "braised chicken"),
    "黑椒牛柳": ("beef dish", "black pepper beef"), "洋葱炒牛肉": ("beef dish", "beef onion"),
    "西兰花炒虾仁": ("broccoli shrimp", "shrimp dish"), "番茄龙利鱼": ("fish fillet", "tomato fish"),
    "蒜蓉粉丝蒸娃娃菜": ("steamed cabbage", "napa cabbage"), "剁椒蒸鱼": ("steamed fish", "fish dish"),
    "萝卜排骨汤": ("pork rib soup", "radish soup"), "玉米排骨汤": ("pork rib soup", "corn soup"),
    "山药排骨汤": ("pork rib soup", "yam soup"), "白菜豆腐汤": ("tofu soup", "cabbage soup"),
    "菌菇汤": ("mushroom soup", "mushroom dish"), "鲫鱼豆腐汤": ("fish soup", "tofu soup"),
    "西葫芦炒鸡蛋": ("scrambled eggs", "zucchini"), "凉拌海带丝": ("seaweed salad", "seaweed dish"),
    "红烧茄子": ("braised eggplant", "eggplant dish"), "水煮肉片": ("spicy pork", "pork dish"),
    "蒜蓉茄子": ("eggplant dish", "steamed eggplant"), "酸辣汤": ("hot and sour soup", "soup"),
    "板栗烧鸡": ("chicken stew", "braised chicken"), "粉蒸肉": ("steamed pork", "pork dish"),
}
_COOKING_WORDS = {
    "蒸": "steamed", "炒": "stir fry", "煎": "pan fried", "烤": "roasted", "汤": "soup",
    "粥": "porridge", "炖": "stew", "焖": "braised", "凉拌": "cold salad",
}

# 商洛本地菜在图库里几乎找不到对应照片，泛化匹配很容易配成“另一道菜”。
# 这里采用白名单：只有实测匹配得住的才允许出图，其余宁可不显示图片。
_LOCAL_DISHES = {
    "商洛洋芋糍粑", "商洛搅团", "商洛浆水面", "洛南豆腐",
    "洛南核桃馍", "镇安腊肉炒土豆", "商州烩菜配米饭", "商洛柿饼",
}
_LOCAL_PHOTO_OK = {
    "商洛洋芋糍粑",  # 洋芋糍粑 ≈ mashed potato，形态一致
    "商洛浆水面",    # noodle soup，形态一致
    "洛南豆腐",      # tofu dish，形态一致
    "商州烩菜配米饭",  # stew，形态一致（配米饭另说）
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
# 西式快餐/甜点等"菜品形态"标签：家常中餐基本不可能是这些，
# 但只要标签里同时有 bacon、potato 就先被搜出来（例如汉堡）。
# 命中这些形态直接判为不匹配，避免"腊肉炒土豆配汉堡图"。
_WRONG_FORM_TAGS = {
    "burger", "hamburger", "sandwich", "hotdog",
    "pizza", "pasta", "spaghetti", "lasagna", "macaroni",
    "taco", "burrito", "nachos", "quesadilla",
    "sushi", "sashimi",
    "cake", "dessert", "cookie", "pastry", "donut", "bagel", "cupcake", "pudding",
}
_COOKED_TAGS = {
    "food", "dish", "meal", "cuisine", "plate", "bowl", "cooked", "grilled", "roasted",
    "fried", "baked", "steamed", "soup", "rice", "noodles", "pasta", "curry", "barbecue",
    "bbq", "restaurant", "dinner", "lunch", "breakfast",
}
# 成品菜/上桌食物的强信号；泛化搜索时至少命中一个，避免生食材、农田和风景。
_FINISHED_TAGS = {
    "dish", "meal", "plate", "bowl", "cooked", "grilled", "grill", "roasted", "roast",
    "fried", "fry", "baked", "steamed", "soup", "stew", "curry", "barbecue", "bbq",
    "rice", "noodles", "pasta", "cuisine", "restaurant", "dinner", "lunch",
    "delicious", "tasty", "gourmet",
}
SEARCH_CACHE_VERSION = "pixabay-v3"


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
    # 列表卡片不需要 1280px 大图，640px 的 webformat 足够且下载快很多。
    image_url = _safe_pixabay_image_url(
        photo.get("webformatURL") or photo.get("largeImageURL") or photo.get("previewURL")
    )
    if not image_url:
        return None
    preview_url = _safe_pixabay_image_url(photo.get("previewURL")) or image_url
    user = str(photo.get("user") or "Pixabay")[:100]
    user_id = str(photo.get("user_id") or "").strip()
    photographer_url = (
        f"https://pixabay.com/users/{quote(user, safe='')}-{quote(user_id, safe='')}/"
        if user_id.isdigit() else "https://pixabay.com/"
    )
    return {
        "url": image_url,
        "preview_url": preview_url,
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


def _has_bad_tag(photo: dict, query_tokens: set[str] | None = None) -> bool:
    text = str(photo.get("tags") or "").lower()
    if "black and white" in text or "black-and-white" in text or "blackandwhite" in text:
        return True
    tags = _photo_tags(photo)
    if tags & _BAD_TAGS:
        return True
    # 菜品本身就叫汉堡/披萨时（query 里带 burger/pizza），不该被形态黑名单误杀。
    wrong_forms = tags & _WRONG_FORM_TAGS
    if wrong_forms and not wrong_forms <= (query_tokens or set()):
        return True
    return False


def _candidate_score(query: str, photo: dict) -> int:
    tokens = _query_tokens(query)
    tags = _photo_tags(photo)
    # 匹配上菜名是基础分；带 food/dish/meal/plate 等成品菜标签的优先，
    # 但标签数量不能无限压过更精确的菜名，所以最多加 3 分。
    return len(tokens & tags) * 2 + min(len(tags & _COOKED_TAGS), 3)


def _matches_query(query: str, photo: dict, require_finished: bool = False) -> bool:
    tokens = _query_tokens(query)
    tags = _photo_tags(photo)
    if not tokens or not tokens.issubset(tags) or _has_bad_tag(photo, tokens):
        return False
    if require_finished and not (tags & _FINISHED_TAGS):
        return False
    return True


def _required_score(query: str) -> int:
    tokens = _query_tokens(query)
    return 2 if len(tokens) == 1 else len(tokens) * 2


async def _search_pixabay(
    cache_key: str,
    variants: list[str],
    key: str,
    require_finished: bool,
) -> dict | None:
    async def search_variant(rank: int, search_query: str) -> list[dict]:
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
        try:
            response = await _client_for_requests().get(PIXABAY_API, params=params)
            response.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return []
        found: list[dict] = []
        for item in response.json().get("hits", []):
            photo = _photo_from_payload(item)
            if photo and _matches_query(search_query, photo, require_finished):
                photo["score"] = _candidate_score(search_query, photo)
                photo["rank"] = rank
                photo["query"] = search_query
                found.append(photo)
        return found

    try:
        # 先搜最精确的变体，命中就直接用；常见菜只需一次 Pixabay 请求。
        candidates = await search_variant(0, variants[0])
        if not candidates and len(variants) > 1:
            # 没命中时才并发搜索备用变体，避免慢请求拖住首次出图。
            groups = await asyncio.gather(
                *(search_variant(rank, query) for rank, query in enumerate(variants[1:], start=1))
            )
            candidates = [photo for group in groups for photo in group]
        best = max(
            candidates,
            key=lambda photo: (photo.get("score", 0), -photo.get("rank", 0)),
            default=None,
        )
        if best is None or best.get("score", 0) < _required_score(best.get("query", variants[0])):
            best = None
        _cache_set(_search_cache, cache_key, (best,), SEARCH_TTL)
        return best
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        _cache_set(_search_cache, cache_key, (None,), 60)
        return None


async def _find_photo(name: str) -> dict | None:
    query = _normalized_query(name)
    if query in _LOCAL_DISHES and query not in _LOCAL_PHOTO_OK:
        # 已知对不上的本地菜：直接不出图，比配一张别的菜更体面。
        return None
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
    # 图片代理和图片信息接口可能同时请求同一道菜，合并成一次 Pixabay 搜索。
    task = _search_tasks.get(cache_key)
    if task is not None:
        return await asyncio.shield(task)
    task = asyncio.create_task(
        _search_pixabay(cache_key, variants, key, not bool(_dish_queries(query)))
    )
    _search_tasks[cache_key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _search_tasks.get(cache_key) is task:
            _search_tasks.pop(cache_key, None)


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
        "preview_url": "",
        "image_alt": "",
        "photographer": "",
        "photographer_url": "",
        "credit_name": "",
        "credit_url": "",
    }


def catalog_url(db: Session, name: str) -> str:
    """只查数据库目录，不触发 Pixabay 搜索；供图片代理优先返回 OSS 图片。"""
    normalized_name = _normalized_query(name)
    photo = db.scalar(
        select(RecipePhoto).where(RecipePhoto.recipe_name == normalized_name)
    )
    if photo is not None and _safe_catalog_url(photo.image_url):
        return photo.image_url
    return ""


async def photo_info(db: Session, name: str) -> dict:
    normalized_name = _normalized_query(name)
    photo = db.scalar(
        select(RecipePhoto).where(RecipePhoto.recipe_name == normalized_name)
    )
    if photo is not None and _safe_catalog_url(photo.image_url):
        return {
            "found": True,
            "image_url": photo.image_url,
            "preview_url": photo.image_url,
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
        "preview_url": (
            f"/api/v1/recipe-photo?"
            f"{urlencode({'name': normalized_name, 'preview': '1', 'strict': '1'})}"
        ),
        "image_alt": f"{normalized_name}成品图片",
        "photographer": pixabay_photo["photographer"],
        "photographer_url": pixabay_photo["photographer_url"],
        "credit_name": f"Photo by {pixabay_photo['photographer']} on Pixabay",
        "credit_url": pixabay_photo["pixabay_url"],
    }


async def _iter_bytes(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def stream_photo(
    name: str, preview: bool = False
) -> tuple[AsyncIterator[bytes], str, bool] | None:
    """返回 (字节流, content-type, 是否命中)。没有合适图片时返回 None。

    边下载边转发，不再把整张图读完才回给浏览器：跨境链路下能省掉一整趟传输时间。
    """
    photo = await _find_photo(name)
    if not photo:
        return None
    url = (photo.get("preview_url") if preview else "") or photo["url"]
    cached = _cached_image(url)
    if cached is not None:
        return _iter_bytes(cached[0]), cached[1], True
    if not _safe_pixabay_image_url(url):
        return None
    stack = AsyncExitStack()
    try:
        response = await stack.enter_async_context(
            _client_for_requests().stream("GET", url, follow_redirects=True)
        )
        if response.status_code != 200:
            raise ValueError("upstream status")
        if not _safe_pixabay_image_url(str(getattr(response, "url", url))):
            raise ValueError("untrusted image redirect")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("upstream content type")
    except (httpx.HTTPError, ValueError):
        await stack.aclose()
        return None

    async def body() -> AsyncIterator[bytes]:
        chunks: list[bytes] = []
        size = 0
        complete = True
        try:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    complete = False
                    break
                chunks.append(chunk)
                yield chunk
        finally:
            await stack.aclose()
        # 只有完整读完才进缓存，客户端中途断开时不写入半张图。
        if complete and chunks:
            _cache_image(url, b"".join(chunks), content_type)

    return body(), content_type, True


async def fetch_photo(
    name: str, fallback_only: bool = False, preview: bool = False
) -> tuple[bytes, str, bool]:
    if fallback_only:
        return _placeholder(name), "image/svg+xml", False
    result = await stream_photo(name, preview=preview)
    if result is None:
        return _placeholder(name), "image/svg+xml", False
    iterator, content_type, found = result
    content = b"".join([chunk async for chunk in iterator])
    return content, content_type, found
