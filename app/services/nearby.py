"""附近招牌菜：高德美食 POI 的 business.tag，不是完整菜单。"""

from __future__ import annotations

import logging

import httpx

from app.services.decision import is_blocked

logger = logging.getLogger("fandazi.nearby")

AROUND_URL = "https://restapi.amap.com/v5/place/around"
TEXT_URL = "https://restapi.amap.com/v5/place/text"
REGEOCODE_URL = "https://restapi.amap.com/v3/geocode/regeo"
REGIONAL_CATALOG_VERSION = 3
GENERIC_TAGS = {
    "美食", "餐饮", "中餐", "西餐", "快餐", "餐厅", "饭店", "小吃", "饮品",
    "外卖", "夜宵", "早餐", "午餐", "晚餐", "菜品", "特色菜", "推荐菜", "人气", "网红",
}
SPICY = ("辣", "麻辣", "香锅", "火锅", "水煮", "剁椒", "川味", "毛血旺")
SWEET = ("甜", "糖", "蛋糕", "奶茶", "甜品", "冰淇淋", "双皮奶", "糖水", "布丁", "芋圆", "点心")
LIGHT = ("清蒸", "白灼", "沙拉", "粥", "蒸菜", "菌菇", "蔬菜", "青菜", "豆腐")
SALTY = ("咸", "酱", "卤", "红烧", "炒", "炖", "煲", "盖饭", "炒饭", "烧", "煎", "烤", "焖")
RICE = ("饭", "炒饭", "盖饭", "煲仔", "粥", "米饭")
NOODLE = ("面", "粉", "饺", "馄饨", "米线", "河粉")
CITY_SPECIALTIES = {
    "商洛市": [
        {"dish": "商洛洋芋糍粑", "tastes": ("salty", "spicy"), "staples": ("any",), "heaviness": "simple"},
        {"dish": "商洛搅团", "tastes": ("salty", "spicy"), "staples": ("any",), "heaviness": "simple"},
        {"dish": "商洛浆水面", "tastes": ("salty", "light"), "staples": ("noodle",), "heaviness": "simple"},
        {"dish": "洛南豆腐", "tastes": ("salty", "light"), "staples": ("nocarb", "rice"), "heaviness": "simple"},
        {"dish": "洛南核桃馍", "tastes": ("salty",), "staples": ("any",), "heaviness": "simple"},
        {"dish": "镇安腊肉炒土豆", "tastes": ("salty", "spicy"), "staples": ("rice", "any"), "heaviness": "heavy"},
        {"dish": "商州烩菜配米饭", "tastes": ("salty", "spicy"), "staples": ("rice",), "heaviness": "heavy"},
        {"dish": "商洛柿饼", "tastes": ("sweet",), "staples": ("any",), "heaviness": "simple"},
    ],
}


class NearbyError(Exception):
    pass


def _tags(raw: object) -> list[str]:
    if not isinstance(raw, str):
        return []
    parts = raw.replace("，", ",").replace("、", ",").replace(";", ",").split(",")
    found: list[str] = []
    for part in parts:
        name = part.strip()
        if len(name) < 2 or name in GENERIC_TAGS or name in found:
            continue
        found.append(name)
    return found


def parse_places(payload: dict) -> list[dict]:
    """把高德周边结果收成餐馆及其已标注菜品；tag 不是完整菜单。"""
    if str(payload.get("status")) != "1":
        info = payload.get("info") or "高德没有返回店铺"
        raise NearbyError(str(info))
    places: list[dict] = []
    for poi in payload.get("pois") or []:
        if not isinstance(poi, dict):
            continue
        shop = str(poi.get("name") or "").strip()
        if not shop:
            continue
        business = poi.get("business") if isinstance(poi.get("business"), dict) else {}
        meters = str(poi.get("distance") or "").strip()
        distance = int(meters) if meters.isdigit() else 99999
        for dish in _tags(business.get("tag")):
            places.append({"shop": shop, "dish": dish, "meters": distance, "source": "nearby"})
    places.sort(key=lambda item: item["meters"])
    return places


def fetch_places(lng: float, lat: float, key: str) -> list[dict]:
    if not key:
        raise NearbyError("服务端还没配置高德 Web 服务 Key，请在项目根目录 .env 设置 AMAP_KEY 并重启后端")
    try:
        response = httpx.get(
            AROUND_URL,
            params={
                "key": key,
                "location": f"{lng:.6f},{lat:.6f}",
                "types": "050000",
                "radius": 3000,
                "page_size": 25,
                "show_fields": "business",
                "sortrule": "distance",
            },
            timeout=8.0,
        )
        response.raise_for_status()
        return parse_places(response.json())
    except NearbyError:
        raise
    except Exception as exc:
        logger.warning("nearby_search_failed %s", type(exc).__name__)
        raise NearbyError("附近的店暂时没查到") from exc


def fetch_city_places(city: str, key: str) -> list[dict]:
    """按城市搜索特色美食 POI；城市搜索失败不影响定位和附近推荐。"""
    if not city or not key:
        return []
    try:
        response = httpx.get(
            TEXT_URL,
            params={
                "key": key,
                "keywords": "特色美食",
                "city": city,
                "citylimit": "true",
                "types": "050000",
                "page_size": 25,
                "show_fields": "business",
            },
            timeout=8.0,
        )
        response.raise_for_status()
        return parse_places(response.json())
    except Exception as exc:
        logger.warning("nearby_city_search_failed %s", type(exc).__name__)
        return []


def _area_name(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return next((part.strip() for part in value if isinstance(part, str) and part.strip()), "")
    return ""


def fetch_location_label(lng: float, lat: float, key: str) -> str:
    """用已授权的坐标反查市和区县；失败时不影响附近店铺推荐。"""
    if not key:
        return ""
    try:
        response = httpx.get(
            REGEOCODE_URL,
            params={
                "key": key,
                "location": f"{lng:.6f},{lat:.6f}",
                "extensions": "base",
            },
            timeout=4.0,
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("status")) != "1":
            return ""
        component = ((payload.get("regeocode") or {}).get("addressComponent") or {})
        city = _area_name(component.get("city"))
        province = _area_name(component.get("province"))
        if not city and province.endswith("市"):
            city = province
        district = _area_name(component.get("district"))
        parts = [name for name in (city, district) if name]
        return " · ".join(dict.fromkeys(parts))
    except Exception as exc:
        logger.warning("nearby_reverse_geocode_failed %s", type(exc).__name__)
        return ""


def regional_places(location_label: str) -> list[dict]:
    city = (location_label or "").split("·", 1)[0].strip()
    return [
        {
            "dish": item["dish"],
            "meters": 99999,
            "region": city,
            "source": "local",
            "tastes": item["tastes"],
            "staples": item["staples"],
            "heaviness": item.get("heaviness"),
        }
        for item in CITY_SPECIALTIES.get(city, [])
    ]


def sources_with_matches(places: list[dict], answers: dict, blocked: list[str]) -> list[str]:
    sources = []
    for source in ("local", "nearby"):
        if choose(places, answers, blocked, [], source=source):
            sources.append(source)
    return sources


def _hit(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _fits(dish: str, answers: dict, place: dict | None = None) -> bool:
    taste = answers.get("taste") or ""
    staple = answers.get("staple") or ""
    tastes = place.get("tastes") if place else None
    staples = place.get("staples") if place else None
    heaviness = place.get("heaviness") if place else None
    if taste and taste != "any":
        if tastes is not None:
            if taste not in tastes:
                return False
        elif taste == "spicy" and not _hit(dish, SPICY):
            return False
        elif taste == "sweet" and not _hit(dish, SWEET):
            return False
        elif taste == "light" and (not _hit(dish, LIGHT) or _hit(dish, SPICY)):
            return False
        elif taste == "salty" and not _hit(dish, SALTY):
            return False
    if staple and staple != "any":
        if staples is not None:
            if (staple == "nocarb" and "any" in staples) or (
                "any" not in staples and staple not in staples
            ):
                return False
        elif staple == "rice" and not _hit(dish, RICE):
            return False
        elif staple == "noodle" and not _hit(dish, NOODLE):
            return False
        elif staple == "nocarb" and _hit(dish, RICE + NOODLE):
            return False
    want_heavy = answers.get("heaviness") or ""
    if want_heavy and heaviness and want_heavy != heaviness:
        return False
    if want_heavy and not heaviness:
        if want_heavy == "heavy" and _hit(dish, ("粥", "甜品", "小吃", "沙拉", "凉菜")):
            return False
        if want_heavy == "simple" and _hit(dish, ("火锅", "套餐", "大盘", "全家桶")):
            return False
    return True


def label_of(place: dict) -> str:
    if place.get("region"):
        return f"{place['region']}特色：{place['dish']}"
    return f"附近{place['shop']}的{place['dish']}"


def choose(places: list[dict], answers: dict, blocked: list[str], drawn: list[str],
           source: str = "all") -> dict | None:
    """返回最近的一道符合口味和忌口的招牌菜。没有就返回 None。"""
    skipped = set(drawn or [])
    for place in places:
        if source != "all" and place.get("source", "nearby") != source:
            continue
        dish = str(place.get("dish") or "")
        shop = str(place.get("shop") or "")
        if not dish or not _fits(dish, answers, place):
            continue
        if is_blocked(dish, blocked) or (shop and is_blocked(shop, blocked)):
            continue
        label = label_of(place)
        if label in skipped:
            continue
        region = place.get("region")
        if region:
            return {
                "choice": label,
                "reasons": [f"{region}地方特色", "符合你选择的口味"],
            }
        meters = place.get("meters")
        distance = f"约 {meters} 米" if isinstance(meters, int) and meters < 90000 else "附近"
        return {
            "choice": label,
            "reasons": [distance, f"{shop}的招牌菜"],
        }
    return None


def alternatives(places: list[dict], answers: dict, blocked: list[str],
                 drawn: list[str], limit: int = 3, source: str = "all") -> list[str]:
    names: list[str] = []
    skipped = set(drawn or [])
    for place in places:
        if source != "all" and place.get("source", "nearby") != source:
            continue
        dish = str(place.get("dish") or "")
        shop = str(place.get("shop") or "")
        if not dish or not _fits(dish, answers, place):
            continue
        if is_blocked(dish, blocked) or (shop and is_blocked(shop, blocked)):
            continue
        label = label_of(place)
        if label in skipped or label in names:
            continue
        names.append(label)
        if len(names) >= limit:
            break
    return names
