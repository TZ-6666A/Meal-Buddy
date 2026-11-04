"""地区饮食采集：按城市地标扫描周边餐饮 POI，把"店 + 实售菜品"整理成结构化数据。

产出走两步：本脚本只生成候选清单（harvest/<城市>.json），人工筛选后
再固化进 app/services/nearby.py 的静态菜库——高德数据不直接进服务。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.services.nearby import LIGHT, NOODLE, RICE, SALTY, SPICY, SWEET  # noqa: E402

TEXT_URL = "https://restapi.amap.com/v5/place/text"
AROUND_URL = "https://restapi.amap.com/v5/place/around"
# 套餐/饮品/营销词：不是"菜"，固化菜库时剔除
NOISE_WORDS = ("双人餐", "单人餐", "套餐", "优惠券", "代金券", "霸王餐", "秒杀",
               "咖啡", "美式", "拿铁", "摩卡", "果茶", "奶茶", "奶昔", "柠檬水",
               "可乐", "雪碧", "果汁", "酸梅汤", "冰粉", "饮品")
# 川菜馆、猪脚饭、小吃城这类"日常馆子"的类目/店名词，用于统计验证
DAILY_HINTS = ("川菜", "湘菜", "猪脚饭", "快餐", "小吃", "面馆", "米线", "麻辣烫",
               "烧烤", "烤肉", "火锅", "盖浇", "炒菜", "家常菜", "砂锅", "饺子", "粥")


def read_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("AMAP_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    sys.exit("!! .env 里没有 AMAP_KEY")


def locate(client: httpx.Client, key: str, city: str, keyword: str) -> tuple[str, str] | None:
    """地标名 -> (坐标, 命中名)。"""
    r = client.get(TEXT_URL, params={
        "key": key, "keywords": keyword, "city": city, "city_limit": "true",
        "show_fields": "business", "page_size": 1,
    }, timeout=10)
    for poi in r.json().get("pois") or []:
        loc = str(poi.get("location") or "").strip()
        if loc:
            return loc, str(poi.get("name") or keyword)
    return None


def scan_area(client: httpx.Client, key: str, loc: str, pages: int) -> list[dict]:
    """单坐标翻页扫描，返回原始 POI 列表。"""
    pois: list[dict] = []
    for page in range(1, pages + 1):
        r = client.get(AROUND_URL, params={
            "key": key, "location": loc, "types": "050000", "radius": 3000,
            "page_size": 25, "page_num": page, "show_fields": "business",
            "sortrule": "distance",
        }, timeout=10)
        batch = r.json().get("pois") or []
        pois.extend(batch)
        if len(batch) < 25:
            break
        time.sleep(0.25)
    return pois


def split_tags(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    parts = raw.replace("，", ",").replace("、", ",").replace(";", ",").replace(":", ",").split(",")
    out: list[str] = []
    for name in parts:
        name = name.strip()
        if len(name) >= 2 and name not in out:
            out.append(name)
    return out


def label(poi: dict, dishes: list[str]) -> dict:
    """按类目链 + 菜名关键词投票给口味/主食/分量打标。"""
    text = " ".join([str(poi.get("type") or ""), str(poi.get("name") or "")] + dishes)
    votes = {"spicy": 0, "sweet": 0, "light": 0, "salty": 0}
    for word in ("川菜", "湘菜", "麻辣烫", "火锅", "串串", "水煮"):
        if word in text:
            votes["spicy"] += 2
    for taste, words in (("spicy", SPICY), ("sweet", SWEET), ("light", LIGHT), ("salty", SALTY)):
        votes[taste] += sum(text.count(w) for w in words)
    taste = max(votes, key=votes.get) if max(votes.values()) > 0 else "any"
    staple = "noodle" if any(w in text for w in NOODLE + ("米粉", "面馆")) else \
             "rice" if any(w in text for w in RICE + ("快餐", "食堂")) else "any"
    heavy = "heavy" if any(w in text for w in ("火锅", "烤肉", "自助", "大盘", "炖菜")) else \
            "simple" if any(w in text for w in ("粥", "沙拉", "轻食", "甜品", "蛋糕")) else ""
    return {"taste": taste, "staple": staple, "heaviness": heavy}


def harvest(key: str, city: str, landmarks: list[str], pages: int) -> dict:
    pois_by_shop: dict[str, dict] = {}
    with httpx.Client() as client:
        for kw in landmarks:
            hit = locate(client, key, city, kw)
            if not hit:
                print(f"  ! 没搜到地标「{kw}」，跳过")
                continue
            loc, name = hit
            print(f"  · {name} ({loc}) 扫描中…")
            for poi in scan_area(client, key, loc, pages):
                shop = str(poi.get("name") or "").strip()
                if not shop or shop in pois_by_shop:
                    continue
                business = poi.get("business") if isinstance(poi.get("business"), dict) else {}
                dishes = split_tags(business.get("tag"))
                meters = str(poi.get("distance") or "").strip()
                pois_by_shop[shop] = {
                    "shop": shop,
                    "type": str(poi.get("type") or ""),
                    "meters": int(meters) if meters.isdigit() else None,
                    "dishes": dishes,
                    "label": label(poi, dishes),
                }
            time.sleep(0.25)
    return {"city": city, "landmarks": landmarks, "pois": list(pois_by_shop.values())}


def report(data: dict) -> None:
    pois = data["pois"]
    with_dish = [p for p in pois if p["dishes"]]
    dishes = Counter(d for p in with_dish for d in p["dishes"])
    noise = {d: n for d, n in dishes.items() if any(w in d for w in NOISE_WORDS)}
    clean = {d: n for d, n in dishes.items() if d not in noise}
    daily = [p["shop"] for p in pois if any(h in p["shop"] + p["type"] for h in DAILY_HINTS)]
    print(f"\n===== {data['city']} 采集结果 =====")
    print(f"馆子总数: {len(pois)}（其中 {len(with_dish)} 家带实售菜品标签）")
    print(f"菜品标签: 原始 {len(dishes)} 个 -> 剔除套餐/饮品噪音 {len(noise)} 个 -> 净菜 {len(clean)} 个")
    print(f"日常餐馆（非景区特产类）: {len(daily)} 家，例如: {'、'.join(daily[:8])}")
    flavors = Counter(p["label"]["taste"] for p in pois)
    print(f"口味分布: {dict(flavors)}")
    print("净菜样本:", "、".join(list(clean)[:20]))


def main() -> None:
    ap = argparse.ArgumentParser(description="按城市地标采集周边餐饮店与菜品")
    ap.add_argument("--city", default="商洛")
    ap.add_argument("--landmarks", default="商洛学院,商洛市人民医院,商鞅广场,商州商城",
                     help="逗号分隔的地标名，作为扫描圆心")
    ap.add_argument("--pages", type=int, default=3, help="每个坐标翻几页（每页25家）")
    ap.add_argument("--out", default="", help="输出 JSON 路径，默认 harvest/<城市>.json")
    args = ap.parse_args()

    key = read_key()
    landmarks = [x.strip() for x in args.landmarks.split(",") if x.strip()]
    print(f"采集 {args.city}：{len(landmarks)} 个地标 × {args.pages} 页")
    data = harvest(key, args.city, landmarks, args.pages)
    report(data)
    out = Path(args.out) if args.out else ROOT / "harvest" / f"{args.city}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {out}")


if __name__ == "__main__":
    main()
