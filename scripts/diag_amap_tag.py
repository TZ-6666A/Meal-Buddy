"""诊断：高德周边美食接口的实际产出——POI 数量、business.tag 覆盖率、tag 内容构成。"""
import os, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 从 .env 读 Key，不打印
key = ""
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for line in open(os.path.join(_root, ".env"), encoding="utf-8"):
    if line.strip().startswith("AMAP_KEY="):
        key = line.split("=", 1)[1].strip().strip('"').strip("'")
if not key:
    print("!! .env 里没有 AMAP_KEY"); sys.exit(1)
print(f"AMAP_KEY 已读取 (长度{len(key)})\n")

GENERIC_TAGS = {
    "美食", "餐饮", "中餐", "西餐", "快餐", "餐厅", "饭店", "小吃", "饮品",
    "外卖", "夜宵", "早餐", "午餐", "晚餐", "菜品", "特色菜", "推荐菜", "人气", "网红",
}
# 粗略的风味/经营词表（用于给幸存tag分类）
FLAVOR_HINTS = ("辣", "麻", "甜", "咸", "清淡", "酸", "香锅", "烤", "涮", "煲", "卤", "酱", "拌", "蒸", "炒", "炸", "炖", "煮")

PLACES = [
    ("商洛市中心", "109.941722,33.868629"),
    ("西安钟楼", "108.943,34.261"),
    ("北京国贸", "116.461,39.908"),
]

def probe(label, loc, page_num=1):
    r = httpx.get("https://restapi.amap.com/v5/place/around", params={
        "key": key, "location": loc, "types": "050000", "radius": 3000,
        "page_size": 25, "page_num": page_num, "show_fields": "business", "sortrule": "distance",
    }, timeout=10)
    data = r.json()
    if str(data.get("status")) != "1":
        return {"label": label, "error": f"{data.get('infocode')} {data.get('info')}"}
    pois = data.get("pois") or []
    total = data.get("count")
    with_tag = [p for p in pois if isinstance(p.get("business"), dict) and (p["business"].get("tag") or "").strip()]
    all_raw_tags, survivors = [], []
    for p in with_tag:
        raw = p["business"].get("tag") or ""
        all_raw_tags.append(raw)
        for part in raw.replace("，", ",").replace("、", ",").replace(";", ",").replace(":", ",").split(","):
            name = part.strip()
            if len(name) >= 2 and name not in GENERIC_TAGS and name not in survivors:
                survivors.append(name)
    flavor = [t for t in survivors if any(h in t for h in FLAVOR_HINTS)]
    return {
        "label": label, "total_in_area": total, "page_pois": len(pois),
        "pois_with_tag": len(with_tag),
        "tag_coverage": f"{len(with_tag)}/{len(pois)}",
        "raw_tag_samples": all_raw_tags[:6],
        "survivor_count": len(survivors),
        "survivors_sample": survivors[:15],
        "flavor_like": len(flavor),
    }

for label, loc in PLACES:
    res = probe(label, loc)
    print(f"===== {label} =====")
    for k, v in res.items():
        print(f"  {k}: {v}")
    print()

# 第二页验证翻页空间
res2 = probe("西安钟楼(第2页)", "108.943,34.261", page_num=2)
print("===== 西安第2页 =====")
for k in ("page_pois", "pois_with_tag", "survivor_count"):
    print(f"  {k}: {res2.get(k)}")
