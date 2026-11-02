"""Explainable nutrition summaries from the meal descriptions the user recorded."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta


MEAL_TYPES = ("早餐", "午餐", "晚餐")
# 早餐可缺，漏餐提醒只看午餐和晚餐。
MAIN_MEALS = ("午餐", "晚餐")
WEEK_MIN_MEALS = 2
WEEK_SKIPPED_MAIN_MEALS = 3
TYPE_ALIASES = {
    "早餐": "早餐", "早饭": "早餐", "breakfast": "早餐",
    "午餐": "午餐", "午饭": "午餐", "lunch": "午餐",
    "晚餐": "晚餐", "晚饭": "晚餐", "dinner": "晚餐",
}

CATEGORY_ORDER = ("主食", "蔬菜", "蛋白质", "奶类", "水果")
CATEGORY_HINTS = {
    "主食": ("米饭", "炒饭", "饭", "粥", "面", "粉", "馒头", "面包", "饺子", "包子", "年糕", "玉米", "红薯", "土豆", "盖饭", "煲仔饭", "米线", "饼", "馍"),
    "蔬菜": ("菜", "蔬菜", "白菜", "青菜", "菠菜", "生菜", "西兰花", "番茄", "西红柿", "黄瓜", "茄子", "胡萝卜", "豆角", "蘑菇", "菌菇", "芹菜", "南瓜"),
    "水果": ("水果", "苹果", "香蕉", "橙", "橘", "梨", "葡萄", "草莓", "西瓜", "芒果", "猕猴桃"),
    "蛋白质": ("鸡蛋", "蛋", "鸡肉", "鸡胸", "鸡腿", "鸡翅", "牛肉", "牛腩", "猪肉", "猪脚", "猪蹄", "排骨", "羊肉", "鸭", "鱼", "虾", "蟹", "贝类", "海鲜", "肉", "豆腐", "豆制品", "牛奶", "酸奶", "奶酪"),
    "奶类": ("牛奶", "酸奶", "奶酪", "奶制品"),
}
RISK_HINTS = {
    "高油线索": ("油炸", "炸", "薯条", "炸鸡", "肥肉", "烧烤", "奶茶", "酥", "锅包肉"),
    "高糖线索": ("奶茶", "蛋糕", "甜点", "糖水", "冰淇淋", "可乐", "汽水", "果汁"),
    "高盐线索": ("腌", "腊肉", "咸菜", "方便面", "火腿", "卤味", "重口", "辣条"),
}

# 用户明确表示“这顿没吃”时的整条描述标记。
SKIP_TOKENS = frozenset({
    "无", "没有", "没", "没吃", "没有吃", "未吃", "不吃", "跳过",
    "没胃口", "不饿", "空", "无记录",
})
# 以这些词开头、且没有可识别食物信息时，也视为明确跳过。
SKIP_PREFIXES = ("没有吃", "没吃", "未吃", "跳过")
_SKIP_PUNCTUATION = re.compile(
    r"[\s，,。.、；;：:！!？?（）()【】\[\]《》<>\"'“”‘’…·~～/\-—–_]+"
)

# 保守的粗热量参考：每命中一个常见食物关键词，按一份常见份量估算。
# 这些数字只是数量级参考，不追求精确；宁可少算，也不把普通一餐误判成超标。
CALORIE_HINTS = {
    # 主食类
    "蛋炒饭": 500, "炒饭": 450, "全麦面包": 180, "面包": 250, "米饭": 230,
    "方便面": 450, "面条": 350, "米粉": 350, "面": 330, "粉": 330,
    "粥": 130, "馒头": 220, "饺子": 400, "包子": 230, "年糕": 250,
    "玉米": 130, "红薯": 130, "土豆": 150, "燕麦": 150, "麦片": 150,
    "猪脚饭": 650, "卤肉饭": 600, "盖饭": 550, "煲仔饭": 550, "便当": 550,
    "臊子面": 450, "炒面": 480, "拌面": 420, "米线": 350, "黄焖鸡": 500,
    # 蛋白质类
    "鸡蛋": 80, "鸡胸": 160, "鸡肉": 200, "牛肉": 250, "猪肉": 300,
    "羊肉": 280, "鱼": 180, "虾": 100, "豆腐": 120, "豆浆": 100,
    "牛奶": 150, "酸奶": 120, "奶酪": 200, "坚果": 180,
    # 蔬果类
    "蔬菜": 40, "青菜": 40, "白菜": 30, "菠菜": 30, "西兰花": 50,
    "生菜": 25, "番茄": 30, "西红柿": 30, "黄瓜": 20, "茄子": 60,
    "胡萝卜": 40, "豆角": 60, "蘑菇": 30, "菌菇": 30, "芹菜": 30,
    "南瓜": 60, "水果": 60, "苹果": 90, "香蕉": 100, "橙": 60,
    "橘": 50, "梨": 80, "葡萄": 90, "草莓": 50, "西瓜": 80,
    "芒果": 100, "猕猴桃": 60, "沙拉": 150,
    # 高热量或风险线索
    "油条": 250, "油炸": 450, "炸鸡": 500, "薯条": 320, "肥肉": 450,
    "烧烤": 400, "奶茶": 350, "蛋糕": 350, "甜点": 300, "糖水": 200,
    "冰淇淋": 250, "可乐": 140, "汽水": 140, "果汁": 120, "火腿": 150,
    "火锅": 700, "披萨": 600, "汉堡": 550,
}
_CALORIE_PATTERN = re.compile(
    "|".join(re.escape(word) for word in sorted(CALORIE_HINTS, key=len, reverse=True))
)
CALORIE_HIGH_THRESHOLD = 2200

# 用来判断“跳过”描述里是否仍带着可识别的食物信息。
FOOD_WORDS = frozenset(
    word
    for hints in (*CATEGORY_HINTS.values(), *RISK_HINTS.values())
    for word in hints
) | frozenset(CALORIE_HINTS)


def _is_skip_description(text: object) -> bool:
    """整条描述都是跳过语义，或开头明确跳过且没有食物信息时，视为 skipped。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    compact = _SKIP_PUNCTUATION.sub("", raw).lower()
    if not compact:
        # 只有空白、破折号或斜杠等标点，例如 "-"、"—"、"/"。
        return True
    if compact in SKIP_TOKENS:
        return True
    if not compact.startswith(SKIP_PREFIXES):
        return False
    # “没吃早餐，吃了面包”这类仍带食物信息的描述按吃过处理。
    return not any(word in compact for word in FOOD_WORDS)


def meal_status(texts: list[str]) -> str:
    """按餐返回 eaten / skipped / missing。"""
    if not texts:
        return "missing"
    if any(not _is_skip_description(text) for text in texts):
        return "eaten"
    return "skipped"


def _day_statuses(grouped: dict[date, dict[str, list[str]]], day: date) -> dict[str, str]:
    return {
        meal_type: meal_status(grouped.get(day, {}).get(meal_type, []))
        for meal_type in MEAL_TYPES
    }

MISSING_MEAL_BASE = {
    "早餐": "早餐还没记录，如果还没吃，建议来一份主食加蛋白质，比如全麦面包配鸡蛋或牛奶。",
    "午餐": "午餐还没记录，建议保证一份主食、一份蔬菜和一份蛋白质。",
    "晚餐": "晚餐还没记录，建议清淡些，补一份蔬菜和蛋白质，主食按需减量。",
}


def normalize_meal_type(value: object, eaten_at=None) -> str:
    normalized = TYPE_ALIASES.get(str(value or "").strip().lower(), "")
    if normalized:
        return normalized
    if eaten_at is not None:
        hour = getattr(eaten_at, "hour", 12)
        return "早餐" if hour < 10 else "午餐" if hour < 15 else "晚餐"
    return ""


def _entry_date(entry) -> date:
    value = entry.eaten_at
    return value.date() if hasattr(value, "date") else value


def _group(entries) -> dict[date, dict[str, list[str]]]:
    grouped: dict[date, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        meal_type = normalize_meal_type(entry.meal_type, entry.eaten_at)
        if not meal_type:
            continue
        description = str(entry.description or "").strip()
        if description:
            grouped[_entry_date(entry)][meal_type].append(description)
    return grouped


def _missing(grouped: dict[date, dict[str, list[str]]], required_dates: list[date]) -> list[dict[str, str]]:
    return [
        {"date": day.isoformat(), "meal_type": meal_type}
        for day in required_dates
        for meal_type in MEAL_TYPES
        if _day_statuses(grouped, day)[meal_type] == "missing"
    ]


def _skipped(grouped: dict[date, dict[str, list[str]]], required_dates: list[date]) -> list[dict[str, str]]:
    return [
        {"date": day.isoformat(), "meal_type": meal_type}
        for day in required_dates
        for meal_type in MEAL_TYPES
        if _day_statuses(grouped, day)[meal_type] == "skipped"
    ]


def meal_reminders(
    grouped: dict[date, dict[str, list[str]]], required_dates: list[date], period: str
) -> list[str]:
    """午餐或晚餐明确标成没吃时提醒。未填写不算没吃，早餐不计入。"""
    skips = [
        item for item in _skipped(grouped, required_dates)
        if item["meal_type"] in MAIN_MEALS
    ]
    if period == "day":
        if not skips:
            return []
        names = "、".join(item["meal_type"] for item in skips)
        return [f"{names}没吃，记得好好吃饭哦。"]
    if len(skips) >= WEEK_SKIPPED_MAIN_MEALS:
        return [
            f"这周有 {len(skips)} 顿午餐或晚餐没吃，每周营养摄取量可能不足，记得好好吃饭哦。"
        ]
    return []


def _week_gaps(
    grouped: dict[date, dict[str, list[str]]], required_dates: list[date]
) -> list[dict]:
    """每天不足 WEEK_MIN_MEALS 餐时的缺口，只统计吃过的餐次。"""
    gaps: list[dict] = []
    for day in required_dates:
        statuses = _day_statuses(grouped, day)
        eaten = [
            meal_type for meal_type in MEAL_TYPES if statuses[meal_type] == "eaten"
        ]
        if len(eaten) >= WEEK_MIN_MEALS:
            continue
        gaps.append({
            "date": day.isoformat(),
            "meals": len(eaten),
            "needed": WEEK_MIN_MEALS,
            "missing_meals": [
                meal_type for meal_type in MEAL_TYPES
                if statuses[meal_type] == "missing"
            ],
            "shortfall": WEEK_MIN_MEALS - len(eaten),
        })
    return gaps


def _labels(texts: list[str], hints: dict[str, tuple[str, ...]]) -> set[str]:
    combined = " ".join(texts).lower()
    return {label for label, words in hints.items() if any(word in combined for word in words)}


def _ordered(labels: set[str]) -> list[str]:
    return [label for label in CATEGORY_ORDER if label in labels]


def _structure_sentence(meal_type: str, categories: set[str], risks: set[str]) -> str:
    if categories:
        sentence = f"{meal_type}有{'、'.join(_ordered(categories))}。"
    else:
        sentence = f"{meal_type}的记录里暂时认不出具体的食材类别。"
    absent = [label for label in ("蔬菜", "蛋白质", "水果") if label not in categories]
    if absent:
        sentence += f"暂时没看到{'、'.join(absent)}。"
    if risks:
        sentence += f"另有{'、'.join(sorted(risks))}。"
    return sentence


def _estimate_calories(grouped: dict[date, dict[str, list[str]]], day: date) -> dict:
    """按常见份量做保守估算；识别不全时只给参考值，不触发超标提醒。"""
    recorded_meals = 0
    recognized_meals = 0
    total = 0
    for meal_type in MEAL_TYPES:
        texts = [
            text for text in grouped.get(day, {}).get(meal_type, [])
            if not _is_skip_description(text)
        ]
        if not texts:
            continue
        recorded_meals += 1
        meal_total = 0
        for text in texts:
            for word in _CALORIE_PATTERN.findall(text.lower()):
                meal_total += CALORIE_HINTS[word]
        if meal_total:
            recognized_meals += 1
            total += meal_total

    if not recorded_meals:
        return {
            "calories": None,
            "high": False,
            "uncertain": False,
            "note": "今天还没有可估算的进食记录，暂时无法估算热量。",
        }
    if not recognized_meals:
        return {
            "calories": None,
            "high": False,
            "uncertain": True,
            "note": "菜名里没有可识别的常见食材，暂时无法估算热量，本次不做热量判断。",
        }

    # 取整到 50 千卡，避免给出看似精确的个位数字。
    calories = int(round(total / 50.0) * 50)
    uncertain = recognized_meals < recorded_meals
    if uncertain:
        return {
            "calories": calories,
            "high": False,
            "uncertain": True,
            "note": f"按常见份量粗略估算约 {calories} 千卡；有餐次无法识别食材，估算不完整，本次不做超标判断。",
        }
    if calories >= CALORIE_HIGH_THRESHOLD:
        return {
            "calories": calories,
            "high": True,
            "uncertain": False,
            "note": f"按常见份量粗略估算约 {calories} 千卡，属于偏高范围；只是粗略估算，不是精确值。",
        }
    return {
        "calories": calories,
        "high": False,
        "uncertain": False,
        "note": f"按常见份量粗略估算约 {calories} 千卡；只是粗略估算，不是精确值。",
    }


def _walking_advice(calories: int | None) -> str:
    if calories:
        return f"估算热量偏高（约 {calories} 千卡），如果今天活动量不大，饭后可以散步或走动 20–30 分钟消消食。"
    return "估算热量偏高，如果今天活动量不大，饭后可以散步或走动 20–30 分钟消消食。"


def _missing_meal_advice(meal_type: str, categories: set[str], risks: set[str], calorie_high: bool) -> str:
    base = MISSING_MEAL_BASE[meal_type]
    advice = base
    if "高油线索" in risks:
        advice += "因为已记录的餐食有高油线索，这一餐建议少油烹调、清淡为主。"
    elif "高糖线索" in risks:
        advice += "已记录的餐食有高糖线索，这一餐少喝含糖饮料。"
    if "蔬菜" not in categories and "蔬菜" not in base:
        advice += "记得加一份蔬菜。"
    if "蛋白质" not in categories and "蛋白质" not in base:
        advice += "再搭配鸡蛋、鱼、豆制品或瘦肉等蛋白质。"
    if calorie_high:
        advice += "今天估算热量偏高，这一餐控制一下份量。"
    return advice


def _day_analysis(
    grouped: dict[date, dict[str, list[str]]],
    day: date,
    statuses: dict[str, str],
    estimate: dict,
) -> dict:
    categories_by_meal: dict[str, set[str]] = {}
    risks_by_meal: dict[str, set[str]] = {}
    observations: list[str] = []
    eaten_meals: list[dict] = []
    recorded_types: list[str] = []
    skipped_types: list[str] = []

    for meal_type in MEAL_TYPES:
        status = statuses.get(meal_type, "missing")
        if status == "skipped":
            skipped_types.append(meal_type)
            observations.append(f"{meal_type}没有吃。")
            continue
        if status != "eaten":
            continue
        texts = [
            text for text in grouped.get(day, {}).get(meal_type, [])
            if not _is_skip_description(text)
        ]
        categories = _labels(texts, CATEGORY_HINTS)
        risks = _labels(texts, RISK_HINTS)
        categories_by_meal[meal_type] = categories
        risks_by_meal[meal_type] = risks
        recorded_types.append(meal_type)
        observations.append(_structure_sentence(meal_type, categories, risks))
        eaten_meals.append({
            "meal_type": meal_type,
            "categories": _ordered(categories),
            "risks": sorted(risks),
        })

    all_categories = set().union(*categories_by_meal.values()) if categories_by_meal else set()
    all_risks = set().union(*risks_by_meal.values()) if risks_by_meal else set()
    missing_types = [meal_type for meal_type in MEAL_TYPES if statuses.get(meal_type) == "missing"]
    high = bool(estimate.get("high"))
    suggestions: list[str] = []
    calorie_suggestion = _walking_advice(estimate.get("calories")) if high else ""
    missing_meals = [
        {
            "meal_type": meal_type,
            "advice": _missing_meal_advice(meal_type, all_categories, all_risks, high),
        }
        for meal_type in missing_types
    ]
    suggestions.extend(item["advice"] for item in missing_meals)
    if calorie_suggestion:
        suggestions.append(calorie_suggestion)

    complete_day = not missing_types
    health_tips: list[str] = []
    if "蔬菜" not in all_categories:
        health_tips.append("已记录的餐食里暂未识别到蔬菜；后续餐次可以补一份新鲜或深色蔬菜。")
    if "蛋白质" not in all_categories:
        health_tips.append("已记录的餐食里暂未识别到明确的蛋白质来源；可以搭配鸡蛋、鱼、豆制品或瘦肉。")
    if "主食" not in all_categories:
        health_tips.append("已记录的餐食里暂未识别到主食；可按饥饿程度搭配适量米饭、全谷物或薯类。")
    if "水果" not in all_categories:
        health_tips.append("记录里暂未看到水果；如果适合你的饮食安排，可以选择一份完整水果。")

    risk_tips = {
        "高油线索": "记录中出现高油食物线索，后续餐次可优先选择蒸、煮等少油做法。",
        "高糖线索": "记录中出现高糖食物线索，可以减少含糖饮料和甜点的频率。",
        "高盐线索": "记录中出现高盐食物线索，后续餐次可少用腌制品和高盐调味料。",
    }
    for risk in ("高油线索", "高糖线索", "高盐线索"):
        if risk in all_risks:
            health_tips.append(risk_tips[risk])
    health_tips.extend([
        "分次适量饮水，并结合天气、活动量和个人情况调整。",
        "尽量保持规律进餐，按饥饿程度安排合适份量。",
    ])

    advantages: list[str] = []
    improvements: list[str] = []
    if complete_day:
        core = {"主食", "蔬菜", "蛋白质", "水果"}
        if core <= all_categories:
            structure = "已记录的餐食结构比较完整。" if skipped_types else "三餐结构比较完整。"
            advantages.append(f"主食、蔬菜、蛋白质和水果都有出现，{structure}")
        elif {"蔬菜", "蛋白质"} <= all_categories:
            advantages.append("蔬菜和蛋白质都有覆盖，基本结构在线。")
        if len(all_categories) >= 4:
            advantages.append("食材种类比较多样，没有只依赖单一食物。")
        balanced = [
            meal_type for meal_type in recorded_types
            if "主食" in categories_by_meal[meal_type] and "蛋白质" in categories_by_meal[meal_type]
        ]
        if len(balanced) >= 2:
            advantages.append(f"有 {len(balanced)} 餐同时包含主食和蛋白质，搭配比较扎实。")
        if not all_risks:
            advantages.append("没有明显的高油、高糖、高盐线索，整体比较清爽。")
        if not advantages:
            if skipped_types:
                advantages.append("已记录的餐食已经能看出当天的饮食轮廓。")
            else:
                advantages.append("三餐都记录了，已经能看出当天的饮食轮廓。")

        if "蔬菜" not in all_categories:
            improvements.append("当天还没有看到蔬菜，可以补一份深色或新鲜蔬菜。")
        if "蛋白质" not in all_categories:
            improvements.append("当天蛋白质来源不足，可以补充鸡蛋、鱼、豆制品或瘦肉。")
        if "主食" not in all_categories:
            improvements.append("当天主食线索较少，注意搭配适量全谷物或薯类。")
        if "水果" not in all_categories:
            improvements.append("当天还没有水果，如果方便可以安排一份。")
        for risk in sorted(all_risks):
            improvements.append(f"当天有{risk}，下次可以少油/少糖/少盐，并注意饮水。")
        if high:
            improvements.append(calorie_suggestion)
        if not improvements:
            improvements.append("暂时没有明显短板，继续留意份量、饮水和规律进餐。")
        suggestions = list(improvements)

    if complete_day and not skipped_types:
        headline = "今天的饮食小结：三餐已记录"
    else:
        parts: list[str] = []
        if recorded_types:
            parts.append(f"已记录{'、'.join(recorded_types)}")
        if skipped_types:
            parts.append(f"{'、'.join(skipped_types)}没吃")
        headline = f"今天的饮食小结（{'，'.join(parts)}）"

    return {
        "headline": headline,
        "mode": "full_day" if complete_day else "partial_day",
        "observations": observations,
        "suggestions": suggestions[:6],
        "health_tips": health_tips,
        "advantages": advantages,
        "improvements": improvements,
        "eaten_meals": eaten_meals,
        "missing_meals": missing_meals,
        "skipped_meals": skipped_types,
        "estimated_calories": estimate.get("calories"),
        "calorie_estimate_high": high,
        "calorie_estimate_uncertain": bool(estimate.get("uncertain")),
        "calorie_note": estimate.get("note", ""),
        "disclaimer": "以上只根据你填写的菜名做结构性提醒，未记录不代表没有吃，也不能替代医生或营养师的判断。",
    }


def _week_analysis(
    grouped: dict[date, dict[str, list[str]]], required_dates: list[date]
) -> dict:
    total_meals = 0
    recorded_days = 0
    breakfast_days = 0
    balanced_meals = 0
    skipped_meals_count = 0
    all_categories: set[str] = set()
    risk_counts: dict[str, int] = defaultdict(int)
    qualified_days = 0
    day_category_counts: list[int] = []

    for day in required_dates:
        statuses = _day_statuses(grouped, day)
        skipped_meals_count += sum(1 for status in statuses.values() if status == "skipped")
        recorded_types = [
            meal_type for meal_type in MEAL_TYPES if statuses[meal_type] == "eaten"
        ]
        total_meals += len(recorded_types)
        if not recorded_types:
            continue
        recorded_days += 1
        if len(recorded_types) >= WEEK_MIN_MEALS:
            qualified_days += 1
        if "早餐" in recorded_types:
            breakfast_days += 1
        categories_today: set[str] = set()
        for meal_type in recorded_types:
            texts = [
                text for text in grouped.get(day, {}).get(meal_type, [])
                if not _is_skip_description(text)
            ]
            categories = _labels(texts, CATEGORY_HINTS)
            risks = _labels(texts, RISK_HINTS)
            categories_today |= categories
            all_categories |= categories
            if {"主食", "蛋白质"} <= categories:
                balanced_meals += 1
            for risk in risks:
                risk_counts[risk] += 1
        day_category_counts.append(len(categories_today))

    required_count = len(required_dates)
    missing_breakfast_days = recorded_days - breakfast_days
    varied_days = sum(1 for count in day_category_counts if count >= 3)
    overview = {
        "required_days": required_count,
        "recorded_days": recorded_days,
        "qualified_days": qualified_days,
        "total_meals": total_meals,
        "min_meals_per_day": WEEK_MIN_MEALS,
    }
    if skipped_meals_count:
        overview["skipped_meals"] = skipped_meals_count
    overview_text = [
        f"记录天数：{recorded_days}/{required_count} 天",
        f"达标情况：{qualified_days}/{required_count} 天达到每天至少 {WEEK_MIN_MEALS} 餐",
        f"餐次总数：{total_meals} 餐",
    ]

    observations = [overview_text[0] + "，" + overview_text[2] + "。"]
    if all_categories:
        observations.append(f"记录里出现了：{'、'.join(_ordered(all_categories))}。")
    observations.append(f"其中有 {breakfast_days} 天记录了早餐。")
    if skipped_meals_count:
        observations.append(f"有 {skipped_meals_count} 餐标记为没吃。")

    advantages: list[str] = []
    if required_count and qualified_days == required_count:
        advantages.append(f"每天都记录到至少 {WEEK_MIN_MEALS} 餐，记录习惯很稳定。")
    if {"蔬菜", "蛋白质"} <= all_categories:
        advantages.append("蔬菜和蛋白质都有覆盖，餐盘结构比较在线。")
    elif "蔬菜" in all_categories or "蛋白质" in all_categories:
        present = "蔬菜" if "蔬菜" in all_categories else "蛋白质"
        advantages.append(f"本周有记录到{present}，基础搭配已经有一个支点。")
    if len(all_categories) >= 4:
        advantages.append(f"一周里出现了 {len(all_categories)} 类食材，食材多样性不错。")
    if recorded_days >= 2 and varied_days >= max(1, (recorded_days + 1) // 2):
        advantages.append(f"有 {varied_days} 天覆盖 3 类以上食材，跨天搭配比较多样。")
    if balanced_meals >= 2:
        advantages.append(f"有 {balanced_meals} 餐同时包含主食和蛋白质，搭配比较扎实。")
    if breakfast_days >= max(1, (required_count + 1) // 2):
        advantages.append(f"有 {breakfast_days} 天记录了早餐，进餐节奏比较规律。")
    if not risk_counts:
        advantages.append("没有明显的高油、高糖、高盐线索，整体比较清爽。")
    if "水果" in all_categories:
        advantages.append("水果也有出现，餐后补充做得不错。")
    if not advantages:
        advantages.append("本周的记录已经能看出整体饮食轮廓。")

    improvements: list[str] = []
    if "蔬菜" not in all_categories:
        improvements.append("本周还没有看到蔬菜，下一周可以尽量每餐补一份深色或新鲜蔬菜。")
    if "蛋白质" not in all_categories:
        improvements.append("本周蛋白质线索不足，可以补充鸡蛋、鱼、豆制品或瘦肉。")
    if "主食" not in all_categories:
        improvements.append("本周主食线索较少，注意搭配适量全谷物或薯类。")
    if "水果" not in all_categories:
        improvements.append("本周还没有水果，如果方便可以每天安排一份。")
    if "奶类" not in all_categories:
        improvements.append("奶类出现得不多，可以按自己的习惯补充牛奶、酸奶或奶酪。")
    if missing_breakfast_days:
        improvements.append(f"有 {missing_breakfast_days} 天没有记录早餐，可以尽量固定早餐时间。")
    for risk in sorted(risk_counts):
        improvements.append(
            f"本周有 {risk_counts[risk]} 餐出现{risk}，下一周可以少油/少糖/少盐，并注意饮水。"
        )
    if total_meals and len(all_categories) <= 2:
        improvements.append("本周能识别出的食材类别较少，可以多换几种蔬菜、蛋白质和主食。")
    elif recorded_days >= 2 and varied_days == 0:
        improvements.append("每天能识别出的食材类别都比较集中，可以换着搭配蔬菜、蛋白质和主食。")
    if not improvements:
        improvements.append("暂时没有明显短板，继续留意份量、饮水和规律进餐。")

    health_tips: list[str] = []
    if "蔬菜" not in all_categories or "蛋白质" not in all_categories:
        health_tips.append("饮食：每餐尽量安排一份蔬菜和一份蛋白质，主食可以部分换成全谷物或薯类。")
    else:
        health_tips.append("饮食：继续保持蔬菜和蛋白质的搭配，主食可以部分换成全谷物或薯类。")
    health_tips.append("饮水：每天分次喝水，以白水为主，天热或活动多时适当增加。")
    health_tips.append("活动：饭后散步或走动 20–30 分钟，每周也可以安排几次让自己微微出汗的活动。")
    if missing_breakfast_days:
        health_tips.append("规律进餐：尽量固定早餐时间，哪怕简单搭配一份主食和蛋白质。")
    else:
        health_tips.append("规律进餐：尽量保持三餐时间相对固定，避免长时间空腹。")

    return {
        "headline": "这周的饮食总结",
        "mode": "full_week",
        "overview": overview,
        "overview_text": overview_text,
        "observations": observations,
        "suggestions": improvements[:5],
        "advantages": advantages,
        "improvements": improvements,
        "health_tips": health_tips,
        "eaten_meals": [],
        "missing_meals": [],
        "estimated_calories": None,
        "calorie_estimate_high": False,
        "calorie_estimate_uncertain": False,
        "calorie_note": "本周总结不做热量估算，热量参考只在当日总结里提供。",
        "disclaimer": "以上只根据你填写的菜名做结构性提醒，未记录不代表没有吃，也不能替代医生或营养师的判断。",
    }


def summarize(entries, period: str, anchor: date, today: date | None = None) -> dict:
    if period not in {"day", "week"}:
        raise ValueError("period 必须是 day 或 week")
    today = today or date.today()
    grouped = _group(entries)
    if period == "day":
        required_dates = [anchor]
    else:
        end = anchor + timedelta(days=6)
        # 当前周只要求填到今天，未来日期不算漏记；历史周要求完整七天。
        last_required = min(end, today) if anchor <= today else anchor - timedelta(days=1)
        required_dates = [anchor + timedelta(days=i) for i in range(max(0, (last_required - anchor).days + 1))]

    missing = _missing(grouped, required_dates) if period == "day" else _week_gaps(grouped, required_dates)
    skipped = _skipped(grouped, required_dates)
    reminders = meal_reminders(grouped, required_dates, period)
    meals = [
        {"date": day.isoformat(), "meal_type": meal_type, "description": description}
        for day in required_dates
        for meal_type in MEAL_TYPES
        for description in grouped.get(day, {}).get(meal_type, [])
    ]
    estimate = (
        _estimate_calories(grouped, anchor)
        if period == "day"
        else {
            "calories": None,
            "high": False,
            "uncertain": False,
            "note": "本周总结不做热量估算，热量参考只在当日总结里提供。",
        }
    )
    result = {
        "period": period,
        "anchor": anchor.isoformat(),
        "complete": not missing and bool(required_dates),
        "required_days": len(required_dates),
        "missing": missing,
        "skipped": skipped,
        "reminders": reminders,
        "meals": meals,
        "analysis": None,
        "estimated_calories": estimate["calories"],
        "calorie_estimate_high": estimate["high"],
        "calorie_estimate_uncertain": estimate["uncertain"],
        "calorie_estimate_note": estimate["note"],
    }

    if period == "day":
        # 只要有真正吃过的餐次就给出当天分析；全部没吃或未记录时保持提示状态。
        statuses = _day_statuses(grouped, anchor)
        if any(status == "eaten" for status in statuses.values()):
            result["analysis"] = _day_analysis(grouped, anchor, statuses, estimate)
    elif result["complete"]:
        result["analysis"] = _week_analysis(grouped, required_dates)
    if result["analysis"] is not None:
        result["analysis"]["reminders"] = reminders
    return result
