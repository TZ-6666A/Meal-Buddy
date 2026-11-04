"""按定位地区提供本地菜候选，并通过高德逆地理编码识别城市。"""

from __future__ import annotations

import logging

import httpx

from app.services.decision import is_blocked

logger = logging.getLogger("fandazi.nearby")

REGEOCODE_URL = "https://restapi.amap.com/v3/geocode/regeo"
IP_LOCATE_URL = "https://restapi.amap.com/v3/ip"
REGIONAL_CATALOG_VERSION = 4
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


def locate_by_ip(ip: str, key: str) -> str:
    """用客户端 IP 粗定位城市。Docker 端口映射后真实 IP 可能丢失，此时返回空。"""
    if not key or not ip:
        return ""
    if ip in ("unknown", "127.0.0.1", "localhost") or ip.startswith(("10.", "192.168.", "172.17.", "172.18.")):
        return ""
    try:
        response = httpx.get(
            IP_LOCATE_URL,
            params={"key": key, "ip": ip},
            timeout=4.0,
        )
        response.raise_for_status()
        payload = response.json()
        if str(payload.get("status")) != "1":
            return ""
        city = _area_name(payload.get("city"))
        province = _area_name(payload.get("province"))
        if not city and province.endswith("市"):
            city = province
        return city
    except Exception as exc:
        logger.warning("nearby_ip_locate_failed %s", type(exc).__name__)
        return ""


def city_label(city: str) -> str:
    """把城市名规范成 location_label 用的形式，便于前端展示与匹配。"""
    name = (city or "").strip()
    if not name:
        return ""
    if not name.endswith(("市", "区", "县", "州", "盟", "地区")):
        name = f"{name}市"
    return name


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
        return str(place["dish"])
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
            if place.get("source") == "ai-city":
                reasons = [f"{region}当地人常吃", "符合你选择的口味"]
            else:
                reasons = [f"来自{region}本地菜库", "符合你选择的口味"]
            return {
                "choice": label,
                "reasons": reasons,
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
