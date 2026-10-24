"""决策引擎：口味问卷 -> 打分排序 -> 遵守忌口的非重复抽取。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

MAX_DRAWS = 5


@dataclass
class Step:
    key: str
    question: str
    options: list[tuple[str, str, str]]


@dataclass
class Mode:
    key: str
    label: str
    steps: list[Step] = field(default_factory=list)


TAKEOUT_STEPS = [
    Step(
        "taste",
        "今天想吃哪种口味？",
        [
            ("sweet", "甜的", "糖醋、蜜汁"),
            ("salty", "咸鲜", "家常、酱香"),
            ("spicy", "辣的", "川湘、麻辣"),
            ("light", "清淡", "少油少盐"),
            ("any", "都行", "你替我挑"),
        ],
    ),
    Step(
        "staple",
        "主食倾向哪一种？",
        [
            ("rice", "米饭", "盖饭、炒饭"),
            ("noodle", "面食", "面、粉、饺子"),
            ("nocarb", "不要碳水", "菜和蛋白质"),
            ("any", "都行", "你帮我决定"),
        ],
    ),
    Step(
        "heaviness",
        "想吃得丰盛，还是简单点？",
        [
            ("heavy", "丰盛", "有肉有菜"),
            ("simple", "简单", "一份就够"),
        ],
    ),
]

COOK_STEPS = [
    Step(
        "time",
        "现在有多少时间做饭？",
        [("15", "15 分钟以内", "快手菜"), ("30", "半小时", "一到两道"), ("60", "一小时以上", "慢慢做")],
    ),
    Step(
        "style",
        "想要什么口感？",
        [("stir", "炒菜", "下饭"), ("soup", "汤水", "暖胃"), ("steam", "蒸煮", "少油烟")],
    ),
]

CONDITIONS: dict[str, dict[str, str]] = {
    "": {"label": "常规", "tip": ""},
    "fitness": {"label": "健身", "tip": "蛋白质给够，主食拳头大小，油控制在两勺以内。"},
    "diabetes": {"label": "控糖", "tip": "主食减半换杂粮，先吃菜再吃饭，避开勾芡和甜口。"},
    "hypertension": {"label": "低盐", "tip": "少盐少酱，避开腌制品，汤底别喝完。"},
}

COND_TAGS: dict[str, set[str]] = {
    "fitness": {"high-protein", "low-fat"},
    "diabetes": {"low-gi", "low-fat"},
    "hypertension": {"low-salt", "low-fat"},
}

# 忌口词 -> 同类食材。用户说“不吃牛肉”，牛腩、肥牛、牛柳也要一起排掉。
BLOCK_ALIASES: dict[str, tuple[str, ...]] = {
    "牛肉": ("牛肉", "牛腩", "牛排", "肥牛", "牛柳", "牛肚", "牛百叶", "牛尾"),
    "猪肉": ("猪肉", "五花", "排骨", "里脊", "叉烧", "猪脚", "猪肚", "肉丝", "肉末", "腊肉", "香肠", "培根", "卤肉", "回锅肉"),
    "羊肉": ("羊肉", "羊排", "羊蝎子", "羊杂"),
    "鸡肉": ("鸡肉", "鸡胸", "鸡腿", "鸡翅", "鸡排", "鸡丁", "鸡丝", "鸡块", "鸡米花", "鸡柳"),
    "鸭肉": ("鸭肉", "烤鸭", "鸭腿", "板鸭", "鸭血"),
    "鱼": ("鱼", "鲈鱼", "三文鱼", "鱼头", "鱼片", "鱼丸"),
    "虾": ("虾", "虾仁", "虾滑"),
    "海鲜": ("海鲜", "虾", "蟹", "贝", "蛤", "鱿鱼", "生蚝", "三文鱼", "鲈鱼", "鱼"),
    "辣": ("辣", "麻辣", "香辣", "酸辣", "水煮", "剁椒", "泡椒", "椒麻", "藤椒"),
    "香菜": ("香菜", "芫荽"),
    "花生": ("花生", "花生米", "花生酱"),
    "鸡蛋": ("鸡蛋", "蛋炒", "蒸蛋", "蛋花", "蛋饺", "卤蛋", "煎蛋", "蛋饼"),
    "牛奶": ("牛奶", "芝士", "奶油", "黄油", "奶盖", "酸奶"),
    "葱": ("葱", "葱花", "洋葱"),
    "蒜": ("蒜", "蒜蓉", "蒜香"),
}

# 菜品库：(名称, 口味集合, 主食, 分量, 标签)
DISHES: list[tuple[str, set[str], str, str, set[str]]] = [
    ("黄焖鸡米饭", {"salty"}, "rice", "heavy", {"high-protein"}),
    ("猪脚饭", {"salty"}, "rice", "heavy", set()),
    ("盐煎肉盖饭", {"salty"}, "rice", "heavy", set()),
    ("酱爆鸡丁饭", {"salty"}, "rice", "heavy", {"high-protein"}),
    ("烤鸭饭", {"salty"}, "rice", "heavy", set()),
    ("鸭腿饭", {"salty"}, "rice", "heavy", set()),
    ("台式卤肉饭", {"salty"}, "rice", "heavy", set()),
    ("腊味煲仔饭", {"salty"}, "rice", "heavy", set()),
    ("排骨年糕", {"salty"}, "rice", "heavy", set()),
    ("咖喱鸡饭", {"salty"}, "rice", "heavy", {"high-protein"}),
    ("番茄牛腩饭", {"salty"}, "rice", "heavy", set()),
    ("水煮牛肉饭", {"spicy"}, "rice", "heavy", set()),
    ("辣子鸡丁饭", {"spicy"}, "rice", "heavy", {"high-protein"}),
    ("剁椒鱼头饭", {"spicy"}, "rice", "heavy", set()),
    ("麻婆豆腐盖饭", {"spicy"}, "rice", "heavy", set()),
    ("宫保鸡丁饭", {"spicy"}, "rice", "heavy", {"high-protein"}),
    ("红烧肉盖饭", {"sweet"}, "rice", "heavy", set()),
    ("糖醋里脊盖饭", {"sweet"}, "rice", "heavy", set()),
    ("糖醋排骨饭", {"sweet"}, "rice", "heavy", set()),
    ("蜜汁叉烧饭", {"sweet"}, "rice", "heavy", set()),
    ("照烧鸡腿饭", {"sweet"}, "rice", "heavy", {"high-protein"}),
    ("清蒸鱼套餐配米饭", {"light"}, "rice", "heavy", {"low-fat", "high-protein"}),
    ("番茄牛腩汤饭", {"light", "salty"}, "rice", "heavy", set()),
    ("虾仁蒸蛋配米饭", {"light"}, "rice", "heavy", {"low-fat", "high-protein"}),
    ("蛋炒饭配紫菜汤", {"salty"}, "rice", "simple", set()),
    ("鸡肉盖饭", {"salty"}, "rice", "simple", {"high-protein"}),
    ("青椒肉丝盖饭", {"salty"}, "rice", "simple", set()),
    ("便利店饭团配豆浆", {"light"}, "rice", "simple", {"low-fat"}),
    ("皮蛋瘦肉粥", {"light"}, "rice", "simple", {"low-fat"}),
    ("鸡肉粥配小菜", {"light"}, "rice", "simple", {"low-fat", "high-protein"}),
    ("酱油炒饭", {"salty"}, "rice", "simple", set()),
    ("煲仔饭小份", {"salty"}, "rice", "simple", set()),
    ("兰州牛肉面", {"salty"}, "noodle", "heavy", set()),
    ("大盘鸡配面", {"spicy"}, "noodle", "heavy", {"high-protein"}),
    ("重庆小面", {"spicy"}, "noodle", "heavy", set()),
    ("酸辣粉", {"spicy"}, "noodle", "heavy", set()),
    ("螺蛳粉", {"spicy"}, "noodle", "heavy", set()),
    ("红烧牛肉面", {"salty"}, "noodle", "heavy", set()),
    ("猪脚面", {"salty"}, "noodle", "heavy", set()),
    ("番茄鸡蛋面", {"salty", "light"}, "noodle", "heavy", {"low-fat"}),
    ("猪肉白菜饺子", {"salty"}, "noodle", "heavy", set()),
    ("酸汤水饺", {"spicy"}, "noodle", "heavy", set()),
    ("鸡丝荞麦面", {"light"}, "noodle", "heavy", {"low-fat", "high-protein", "low-gi"}),
    ("菌菇汤面", {"light"}, "noodle", "heavy", {"low-fat"}),
    ("葱油拌面", {"sweet", "salty"}, "noodle", "heavy", set()),
    ("小馄饨", {"light"}, "noodle", "simple", {"low-fat"}),
    ("清汤面配青菜", {"light"}, "noodle", "simple", {"low-fat"}),
    ("鸡丝凉面", {"salty"}, "noodle", "simple", {"high-protein"}),
    ("牛肉粉丝汤", {"salty"}, "noodle", "simple", set()),
    ("鸡蛋灌饼", {"salty"}, "noodle", "simple", set()),
    ("烤鸡胸配时蔬", {"light"}, "nocarb", "heavy", {"low-fat", "high-protein"}),
    ("白灼虾配西兰花", {"light"}, "nocarb", "heavy", {"low-fat", "high-protein"}),
    ("香煎三文鱼配芦笋", {"light"}, "nocarb", "heavy", {"high-protein"}),
    ("白切鸡拼盘", {"light", "salty"}, "nocarb", "heavy", {"low-fat", "high-protein"}),
    ("口水鸡配黄瓜", {"spicy"}, "nocarb", "heavy", {"high-protein"}),
    ("麻辣香锅", {"spicy"}, "nocarb", "heavy", set()),
    ("猪肚鸡火锅", {"salty"}, "nocarb", "heavy", {"high-protein"}),
    ("蒜蓉西兰花配鸡胸", {"light"}, "nocarb", "heavy", {"low-fat", "high-protein"}),
    ("凉拌黄瓜配白切鸡", {"salty"}, "nocarb", "heavy", {"low-fat", "high-protein"}),
    ("清蒸鲈鱼配时蔬", {"light"}, "nocarb", "heavy", {"low-fat", "high-protein"}),
    ("番茄豆腐汤", {"light"}, "nocarb", "heavy", {"low-fat", "low-salt"}),
    ("冬瓜排骨汤", {"light"}, "nocarb", "heavy", {"low-fat"}),
    ("鸡肉沙拉", {"light"}, "nocarb", "simple", {"low-fat", "high-protein"}),
    ("蔬菜沙拉配鸡胸", {"light"}, "nocarb", "simple", {"low-fat", "high-protein"}),
    ("关东煮", {"light"}, "nocarb", "simple", {"low-fat", "low-salt"}),
    ("茶叶蛋配无糖豆浆", {"light"}, "nocarb", "simple", {"low-fat", "low-gi"}),
    ("凉拌豆腐", {"light"}, "nocarb", "simple", {"low-fat", "low-salt"}),
    ("蒜蓉油麦菜配煎蛋", {"light"}, "nocarb", "simple", {"low-fat"}),
    ("照烧鸡排饭", {"sweet"}, "any", "heavy", {"high-protein"}),
    ("炒河粉", {"salty"}, "any", "heavy", set()),
    ("清汤麻辣烫", {"light", "spicy"}, "any", "heavy", {"low-fat"}),
    ("双层鸡肉堡套餐", {"salty"}, "any", "heavy", set()),
    ("三明治配咖啡", {"light"}, "any", "simple", {"low-fat"}),
    ("鸡肉卷饼", {"salty"}, "any", "simple", {"high-protein"}),
    ("小米粥配鸡蛋", {"light"}, "any", "simple", {"low-fat", "low-gi"}),
    ("红豆汤配全麦面包", {"sweet"}, "any", "simple", {"low-fat", "low-gi"}),
]

# 特殊模式只在该清单里选，避免关键词黑名单漏网。
SAFE_CATALOG: dict[str, list[tuple[str, set[str], str, str, set[str]]]] = {
    "fitness": [
        ("烤鸡胸配时蔬", {"light"}, "nocarb", "heavy", {"high-protein", "low-fat"}),
        ("白灼虾配西兰花", {"light"}, "nocarb", "heavy", {"high-protein", "low-fat"}),
        ("香煎三文鱼配芦笋", {"light"}, "nocarb", "heavy", {"high-protein"}),
        ("虾仁蒸蛋配杂粮饭", {"light"}, "rice", "heavy", {"high-protein", "low-fat"}),
        ("香菇滑鸡配糙米饭", {"salty"}, "rice", "heavy", {"high-protein", "low-gi"}),
        ("番茄牛腩汤配杂粮饭", {"salty"}, "rice", "heavy", {"high-protein"}),
        ("鸡丝荞麦面", {"salty"}, "noodle", "heavy", {"high-protein", "low-gi"}),
        ("蒜蓉西兰花配水煮蛋", {"light"}, "nocarb", "simple", {"low-fat"}),
        ("清蒸鲈鱼配藜麦", {"light"}, "rice", "heavy", {"high-protein", "low-gi"}),
        ("鸡肉蔬菜卷饼", {"salty"}, "any", "simple", {"high-protein"}),
        ("希腊酸奶配蓝莓", {"sweet"}, "any", "simple", {"high-protein", "low-gi"}),
        ("鸡胸肉沙拉碗", {"light"}, "nocarb", "simple", {"high-protein", "low-fat"}),
        ("牛肉西兰花配糙米", {"salty"}, "rice", "heavy", {"high-protein", "low-gi"}),
        ("蒸红薯配水煮蛋", {"sweet"}, "any", "simple", {"low-fat", "low-gi"}),
    ],
    "diabetes": [
        ("清蒸鲈鱼配杂粮饭", {"light"}, "rice", "heavy", {"low-gi", "low-fat"}),
        ("白灼虾配西兰花", {"light"}, "nocarb", "heavy", {"low-gi", "low-fat"}),
        ("番茄豆腐汤配荞麦面", {"salty"}, "noodle", "heavy", {"low-gi", "low-fat"}),
        ("蒜蓉西兰花配鸡胸", {"light"}, "nocarb", "heavy", {"low-gi", "low-fat"}),
        ("香菇滑鸡配糙米", {"salty"}, "rice", "heavy", {"low-gi"}),
        ("凉拌黄瓜配白切鸡", {"salty"}, "nocarb", "heavy", {"low-gi", "low-fat"}),
        ("虾仁蒸蛋配杂粮饭", {"light"}, "rice", "heavy", {"low-gi", "low-fat"}),
        ("清炒时蔬配煎鸡腿", {"salty"}, "nocarb", "simple", {"low-gi"}),
        ("菌菇汤配荞麦面", {"light"}, "noodle", "simple", {"low-gi", "low-fat"}),
        ("番茄牛腩配杂粮饭", {"salty"}, "rice", "heavy", {"low-gi"}),
        ("小米粥配水煮蛋", {"light"}, "any", "simple", {"low-gi", "low-fat"}),
        ("凉拌木耳配鸡丝", {"light"}, "nocarb", "simple", {"low-gi", "low-fat"}),
        ("清蒸南瓜配鸡胸", {"light"}, "nocarb", "simple", {"low-gi", "low-fat"}),
        ("杂粮饭配清炒虾仁", {"salty"}, "rice", "heavy", {"low-gi", "high-protein"}),
    ],
    "hypertension": [
        ("清蒸鲈鱼配米饭", {"light"}, "rice", "heavy", {"low-salt", "low-fat"}),
        ("白灼虾配西兰花", {"light"}, "nocarb", "heavy", {"low-salt", "low-fat"}),
        ("番茄豆腐汤", {"light"}, "nocarb", "simple", {"low-salt", "low-fat"}),
        ("蒜蓉西兰花", {"light"}, "nocarb", "simple", {"low-salt"}),
        ("香菇滑鸡配米饭", {"salty"}, "rice", "heavy", {"low-salt"}),
        ("清炒时蔬配鸡胸", {"light"}, "nocarb", "heavy", {"low-salt", "low-fat"}),
        ("虾仁蒸蛋", {"light"}, "nocarb", "simple", {"low-salt", "low-fat"}),
        ("番茄鸡蛋面少盐", {"light"}, "noodle", "heavy", {"low-salt"}),
        ("冬瓜排骨汤配米饭", {"light"}, "rice", "heavy", {"low-salt", "low-fat"}),
        ("手撕包菜配杂粮饭", {"salty"}, "rice", "heavy", {"low-salt", "low-gi"}),
        ("清蒸南瓜配鸡胸", {"light"}, "nocarb", "simple", {"low-salt", "low-fat"}),
        ("凉拌黄瓜", {"light"}, "nocarb", "simple", {"low-salt", "low-fat"}),
        ("杂粮饭配白灼菜心", {"light"}, "rice", "simple", {"low-salt", "low-gi"}),
        ("香菇青菜面少盐", {"salty"}, "noodle", "heavy", {"low-salt"}),
    ],
}

COOK_DISHES: list[tuple[str, set[str], int, set[str]]] = [
    ("番茄炒蛋", {"stir"}, 15, {"low-fat"}),
    ("青椒肉丝", {"stir"}, 15, set()),
    ("蒜蓉西兰花", {"stir"}, 15, {"low-fat", "low-salt"}),
    ("手撕包菜", {"stir"}, 15, {"low-fat", "low-salt"}),
    ("清炒油麦菜", {"stir"}, 15, {"low-fat", "low-salt"}),
    ("香菇青菜", {"stir"}, 15, {"low-fat"}),
    ("虾仁蒸蛋", {"steam"}, 15, {"low-fat", "high-protein"}),
    ("番茄豆腐汤", {"soup"}, 15, {"low-fat", "low-salt"}),
    ("紫菜蛋花汤", {"soup"}, 15, {"low-fat", "low-salt"}),
    ("凉拌黄瓜", {"stir"}, 15, {"low-fat", "low-salt"}),
    ("蒜香虾仁", {"stir"}, 30, {"high-protein", "low-fat"}),
    ("香菇滑鸡", {"steam", "stir"}, 30, {"high-protein"}),
    ("清蒸鲈鱼", {"steam"}, 30, {"high-protein", "low-fat", "low-salt"}),
    ("西红柿炖豆腐", {"soup"}, 30, {"low-fat"}),
    ("土豆炖鸡", {"soup"}, 60, {"high-protein"}),
    ("冬瓜排骨汤", {"soup"}, 60, {"low-fat", "low-salt"}),
    ("番茄牛腩", {"soup"}, 60, set()),
    ("土豆炖牛肉", {"soup"}, 60, set()),
    ("小鸡炖蘑菇", {"soup"}, 60, {"high-protein"}),
]

TASTE_LABEL = {"sweet": "甜口", "salty": "咸鲜", "spicy": "辣的", "light": "清淡", "any": "口味随意"}
# 忌口词 -> 需要整体排除的口味标签。菜名不带“辣”字的川湘菜靠这个兜住。
BLOCKED_TASTES: dict[str, set[str]] = {
    "辣": {"spicy"}, "麻辣": {"spicy"}, "香辣": {"spicy"}, "酸辣": {"spicy"}, "辣椒": {"spicy"},
    "甜": {"sweet"}, "糖": {"sweet"},
    "油腻": {"sweet"}, "油": set(),
}
STAPLE_LABEL = {"rice": "米饭", "noodle": "面食", "nocarb": "不要主食", "any": "主食随意"}
HEAVY_LABEL = {"heavy": "丰盛一点", "simple": "简单一份"}
TIME_LABEL = {"15": "15 分钟内", "30": "半小时", "60": "一小时以上"}
STYLE_LABEL = {"stir": "炒菜", "soup": "汤水", "steam": "蒸煮"}


def condition_profile(key: str) -> dict[str, str]:
    return CONDITIONS.get(key, CONDITIONS[""])


def mode_for(mode: str) -> Mode:
    if mode == "cook":
        return Mode(key="cook", label="在家做", steps=COOK_STEPS)
    return Mode(key="takeout", label="点外卖", steps=TAKEOUT_STEPS)


def next_step(mode: str, answers: dict[str, str]) -> Step | None:
    for step in mode_for(mode).steps:
        if step.key not in answers:
            return step
    return None


def expand_blocked(words: list[str] | None) -> list[str]:
    """把“牛肉”这类粗粒度忌口展开成具体食材，供名称匹配。"""
    out: list[str] = []
    for word in words or []:
        word = (word or "").strip()
        if not word:
            continue
        if word not in out:
            out.append(word)
        for alias in BLOCK_ALIASES.get(word, ()):
            if alias not in out:
                out.append(alias)
    return out


def is_blocked(name: str, blocked: list[str] | None) -> bool:
    return any(word and word in name for word in (blocked or []))


def is_recent(name: str, recent: list[str] | None) -> bool:
    """最近吃过的不再推荐：菜名出现在记录里，或记录本身是这道菜的简称。"""
    for text in recent or []:
        text = (text or "").strip()
        if not text:
            continue
        if name in text:
            return True
        if len(text) >= 3 and text in name:
            return True
    return False


def _score(tastes: set[str], staple: str, heavy: str, want_taste: str, want_staple: str, want_heavy: str) -> int:
    score = 0
    if want_taste and want_taste != "any":
        score += 4 if want_taste in tastes else -3
    elif want_taste == "any":
        score += 1
    if want_staple and want_staple != "any":
        if want_staple == staple:
            score += 4
        elif staple == "any":
            score += 1
        else:
            score -= 3
    elif want_staple == "any":
        score += 1
    if want_heavy:
        score += 3 if heavy == want_heavy else -1
    return score


def ranked(mode: str, answers: dict[str, str], condition: str,
           blocked: list[str] | None = None, recent: list[str] | None = None,
           exclude: set[str] | None = None) -> list[tuple[str, int]]:
    """返回 (菜名, 分数) 降序列表。忌口和近期记录在这里统一过滤。"""
    banned = expand_blocked(blocked)
    skip = exclude or set()
    banned_tastes: set[str] = set()
    for word in blocked or []:
        banned_tastes |= BLOCKED_TASTES.get((word or "").strip(), set())
    result: list[tuple[str, int]] = []

    if mode == "cook":
        raw = str(answers.get("time", "60") or "60")
        minutes = int(raw) if raw.isdigit() else 60
        style = answers.get("style", "")
        for name, styles, need, tags in COOK_DISHES:
            if need > minutes or name in skip:
                continue
            if is_blocked(name, banned) or is_recent(name, recent):
                continue
            score = 3 if style and style in styles else 0
            score += 2 if need <= max(minutes - 15, 15) else 0
            if condition in COND_TAGS:
                score += 2 if tags & COND_TAGS[condition] else -4
            result.append((name, score))
        result.sort(key=lambda item: -item[1])
        return result

    rows = SAFE_CATALOG[condition] if condition in SAFE_CATALOG else DISHES
    want_taste = answers.get("taste", "")
    want_staple = answers.get("staple", "")
    want_heavy = answers.get("heaviness", "")
    for name, tastes, staple, heavy, tags in rows:
        if name in skip or is_blocked(name, banned) or is_recent(name, recent):
            continue
        if banned_tastes and tastes & banned_tastes:
            continue
        score = _score(tastes, staple, heavy, want_taste, want_staple, want_heavy)
        if condition in COND_TAGS:
            score += 2 if tags & COND_TAGS[condition] else -4
        result.append((name, score))
    result.sort(key=lambda item: -item[1])
    return result


def candidates(mode: str, answers: dict[str, str], condition: str,
               blocked: list[str] | None = None, recent: list[str] | None = None) -> list[str]:
    return [name for name, _ in ranked(mode, answers, condition, blocked, recent)]


def draw(mode: str, answers: dict[str, str], drawn: list[str], condition: str,
         blocked: list[str] | None = None, recent: list[str] | None = None) -> str:
    """只在当前最高分档里抽；这一档抽完了自然退到下一档，忌口全程生效。"""
    ranking = ranked(mode, answers, condition, blocked, recent, set(drawn or []))
    if not ranking:
        # 近期记录太多时放开“最近吃过”，但绝不放宽忌口
        ranking = ranked(mode, answers, condition, blocked, None, set(drawn or []))
    if not ranking:
        return ""
    best = ranking[0][1]
    band = [name for name, score in ranking if score >= best - 1]
    return random.choice(band)


def alternatives(mode: str, answers: dict[str, str], drawn: list[str], condition: str,
                 blocked: list[str] | None = None, recent: list[str] | None = None,
                 limit: int = 3, pick: str = "") -> list[str]:
    """结果卡下方的备选：和主结果同档，换着看也不跑偏。"""
    ranking = ranked(mode, answers, condition, blocked, recent, set(drawn or []))
    if len(ranking) < limit:
        ranking = ranked(mode, answers, condition, blocked, None, set(drawn or []))
    if pick and ranking:
        score_of = {name: score for name, score in ranking}
        target = score_of.get(pick, ranking[0][1])
        same = [name for name, score in ranking if abs(score - target) <= 1]
        if len(same) >= limit:
            ranking = [(name, score) for name, score in ranking if name in set(same)]
    return [name for name, _ in ranking[:limit]]


TASTE_WORDS = {"spicy": "辣", "sweet": "甜", "light": "清淡"}


def taste_conflict(answers: dict[str, str], blocked: list[str] | None) -> str:
    """用户想吃辣，但辣在忌口里：明说出来，别让结果看起来像没听懂。"""
    taste = answers.get("taste", "")
    word = TASTE_WORDS.get(taste, "")
    if not word or not blocked:
        return ""
    if any(taste in BLOCKED_TASTES.get((item or "").strip(), set()) for item in blocked):
        return f"你想吃{word}的，但这条和你的忌口冲突，先给你挑不{word}的。"
    return ""


def reason_line(mode: str, answers: dict[str, str], blocked: list[str] | None) -> str:
    parts: list[str] = []
    if mode == "cook":
        for key, labels in (("time", TIME_LABEL), ("style", STYLE_LABEL)):
            value = answers.get(key, "")
            if value in labels:
                parts.append(labels[value])
    else:
        for key, labels in (("taste", TASTE_LABEL), ("staple", STAPLE_LABEL), ("heaviness", HEAVY_LABEL)):
            value = answers.get(key, "")
            if value not in labels or value == "any":
                continue
            if key == "taste" and any(
                value in BLOCKED_TASTES.get((word or "").strip(), set()) for word in (blocked or [])
            ):
                continue
            parts.append(labels[value])
    reason = ("按你选的 " + "、".join(parts)) if parts else "按你的口味"
    if blocked:
        reason += f"，避开了{'、'.join(blocked[:4])}"
    return reason


def result_line(mode: str, answers: dict[str, str], choice: str, condition: str,
                blocked: list[str] | None = None) -> str:
    reason = reason_line(mode, answers, None)
    head = f"今天就做「{choice}」" if mode == "cook" else f"推荐你吃「{choice}」"
    text = f"{head}，{reason}。"
    conflict = taste_conflict(answers, blocked)
    return f"{conflict}{text}" if conflict else text


COND_TIP = {
    "fitness": "高蛋白、少油，主食按拳头量。",
    "diabetes": "主食换杂粮、减半，先吃菜再吃饭。",
    "hypertension": "少盐少酱，汤别喝完。",
}


def explain(mode: str, answers: dict[str, str], choice: str, condition: str,
            blocked: list[str] | None = None, recent: list[str] | None = None) -> list[str]:
    """把“为什么推这道”拆成几条可核对的短句。"""
    out: list[str] = []
    rows = SAFE_CATALOG.get(condition) or DISHES
    row = next((item for item in rows if item[0] == choice), None)
    if row is not None:
        tastes = row[1] if mode != "cook" else set()
        staple = row[2] if mode != "cook" else ""
        heavy = row[3] if mode != "cook" else ""
        tastes = tastes or set()
        want_taste = answers.get("taste", "")
        want_staple = answers.get("staple", "")
        want_heavy = answers.get("heaviness", "")
        if want_taste and want_taste != "any" and want_taste in tastes:
            out.append(f"对上了你想吃的{TASTE_LABEL.get(want_taste, '')}")
        if want_staple and want_staple != "any" and staple == want_staple:
            out.append(f"主食是{STAPLE_LABEL.get(want_staple, '')}")
        if want_staple == "nocarb" and staple == "nocarb":
            out.append("没有额外主食")
        if want_heavy and heavy == want_heavy:
            out.append("分量合适")
    if mode == "cook":
        raw = str(answers.get("time", "") or "")
        if raw.isdigit():
            out.append(f"{raw} 分钟内能做完")
        style = answers.get("style", "")
        if style in STYLE_LABEL:
            out.append(STYLE_LABEL[style])
    if blocked:
        out.append("避开 " + "、".join(blocked[:3]))
    if condition in COND_TIP:
        out.append(COND_TIP[condition])
    if recent:
        out.append("最近没重复")
    return out[:4]


def exhausted_hint(draws: int, last: str = "") -> str:
    if draws < MAX_DRAWS:
        return ""
    prefix = f"上一次抽到「{last}」，但显然不是你想要的。" if last else ""
    return (
        f"{prefix}已经抽了 {draws} 次，可能你现在不是真的饿。"
        "可以先喝杯水、歇一会儿，过一两个小时再决定。"
    )
