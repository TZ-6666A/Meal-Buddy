from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.services import nutrition


def meal(day, meal_type, description):
    return SimpleNamespace(
        eaten_at=datetime.combine(day, datetime.min.time()),
        meal_type=meal_type,
        description=description,
    )


def test_daily_summary_with_only_breakfast_still_analyzes():
    today = date.today()
    result = nutrition.summarize([meal(today, "早餐", "牛奶和面包")], "day", today, today=today)

    assert result["complete"] is False
    assert {(item["date"], item["meal_type"]) for item in result["missing"]} == {
        (today.isoformat(), "午餐"), (today.isoformat(), "晚餐")
    }
    assert result["analysis"] is not None
    assert "早餐" in result["analysis"]["headline"]
    assert {item["meal_type"] for item in result["analysis"]["missing_meals"]} == {"午餐", "晚餐"}
    advice = " ".join(item["advice"] for item in result["analysis"]["missing_meals"])
    assert "午餐" in advice and "晚餐" in advice
    health_tips = " ".join(result["analysis"]["health_tips"])
    assert "蔬菜" in health_tips and "饮水" in health_tips and "规律进餐" in health_tips


def test_daily_summary_missing_dinner_advice_uses_eaten_context():
    today = date.today()
    entries = [
        meal(today, "早餐", "面包和牛奶"),
        meal(today, "午餐", "炸鸡和薯条"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["analysis"] is not None
    missing = {item["meal_type"]: item["advice"] for item in result["analysis"]["missing_meals"]}
    assert set(missing) == {"晚餐"}
    assert "晚餐" in missing["晚餐"]
    assert "清淡" in missing["晚餐"]
    assert "蔬菜" in missing["晚餐"]
    assert missing["晚餐"].count("蔬菜") == 1
    assert missing["晚餐"].count("蛋白质") == 1


def test_daily_summary_with_all_meals_gives_advantages_and_improvements():
    today = date.today()
    entries = [
        meal(today, "早餐", "牛奶、全麦面包和香蕉"),
        meal(today, "午餐", "米饭、西兰花和鸡胸肉"),
        meal(today, "晚餐", "番茄豆腐汤和米饭"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["complete"] is True
    assert result["missing"] == []
    analysis = result["analysis"]
    assert analysis["mode"] == "full_day"
    assert analysis["advantages"]
    assert analysis["improvements"]
    assert analysis["health_tips"]
    assert any("规律进餐" in item for item in analysis["health_tips"])
    assert any("蔬菜" in item for item in analysis["observations"])
    assert analysis["disclaimer"]


def test_daily_health_tips_flag_logged_oil_sugar_and_salt_clues():
    today = date.today()
    entries = [
        meal(today, "早餐", "油条、奶茶"),
        meal(today, "午餐", "方便面和青菜"),
        meal(today, "晚餐", "米饭和鸡蛋"),
    ]

    analysis = nutrition.summarize(entries, "day", today, today=today)["analysis"]

    tips = " ".join(analysis["health_tips"])
    assert "少油" in tips
    assert "含糖饮料" in tips
    assert "高盐调味料" in tips


def test_daily_summary_warns_about_high_calorie_estimate():
    today = date.today()
    entries = [
        meal(today, "早餐", "油条和豆浆"),
        meal(today, "午餐", "蛋炒饭、炸鸡和奶茶"),
        meal(today, "晚餐", "火锅、蛋糕和可乐"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["estimated_calories"] >= nutrition.CALORIE_HIGH_THRESHOLD
    assert result["estimated_calories"] % 50 == 0
    assert result["calorie_estimate_high"] is True
    combined = " ".join(result["analysis"]["improvements"])
    assert "散步" in combined or "走动" in combined
    assert "粗略估算" in result["calorie_estimate_note"]


def test_daily_summary_without_records_returns_prompt_state():
    today = date.today()
    result = nutrition.summarize([], "day", today, today=today)

    assert result["complete"] is False
    assert result["analysis"] is None
    assert len(result["missing"]) == 3
    assert result["estimated_calories"] is None
    assert result["calorie_estimate_high"] is False


def test_daily_summary_does_not_guess_calories_from_vague_descriptions():
    today = date.today()
    result = nutrition.summarize(
        [meal(today, "早餐", "和朋友吃了顿好的")], "day", today, today=today
    )

    assert result["analysis"] is not None
    assert result["estimated_calories"] is None
    assert result["calorie_estimate_high"] is False
    assert result["calorie_estimate_uncertain"] is True


def test_unspecified_meal_type_uses_time_like_the_calendar():
    today = date.today()
    entries = [
        meal(today, "unspecified", "牛奶和面包"),
        meal(today, "unspecified", "米饭、青菜和鸡肉"),
        meal(today, "unspecified", "豆腐和蔬菜汤"),
    ]
    entries[0].eaten_at = datetime.combine(today, datetime.min.time()).replace(hour=8)
    entries[1].eaten_at = datetime.combine(today, datetime.min.time()).replace(hour=12)
    entries[2].eaten_at = datetime.combine(today, datetime.min.time()).replace(hour=18)

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["complete"] is True


def test_daily_breakfast_skipped_is_not_treated_as_unrecognized_meal():
    today = date.today()
    entries = [
        meal(today, "早餐", "没吃"),
        meal(today, "午餐", "米饭和鸡胸肉"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)
    analysis = result["analysis"]

    assert analysis is not None
    assert analysis["skipped_meals"] == ["早餐"]
    assert "早餐没有吃" in " ".join(analysis["observations"])
    assert "认不出" not in " ".join(analysis["observations"])
    assert "早餐没吃" in analysis["headline"]
    assert {(item["date"], item["meal_type"]) for item in result["missing"]} == {
        (today.isoformat(), "晚餐")
    }
    assert {item["meal_type"] for item in analysis["missing_meals"]} == {"晚餐"}
    assert all("早餐" not in item["advice"] for item in analysis["missing_meals"])
    assert result["estimated_calories"] is not None
    assert result["calorie_estimate_uncertain"] is False


def test_daily_all_meals_skipped_has_no_analysis():
    today = date.today()
    entries = [
        meal(today, "早餐", "没吃"),
        meal(today, "午餐", "没有吃"),
        meal(today, "晚餐", "跳过"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["analysis"] is None
    assert result["missing"] == []
    assert {item["meal_type"] for item in result["skipped"]} == {"早餐", "午餐", "晚餐"}
    assert result["calorie_estimate_uncertain"] is False


def test_daily_skip_marker_with_food_in_same_meal_counts_as_eaten():
    today = date.today()
    entries = [
        meal(today, "早餐", "没吃"),
        meal(today, "早餐", "牛奶和面包"),
        meal(today, "午餐", "米饭和青菜"),
    ]

    analysis = nutrition.summarize(entries, "day", today, today=today)["analysis"]

    assert analysis is not None
    assert analysis["skipped_meals"] == []
    assert "早餐没有吃" not in " ".join(analysis["observations"])
    assert [item["meal_type"] for item in analysis["eaten_meals"]] == ["早餐", "午餐"]


def test_skip_markers_do_not_misfire_on_food_descriptions():
    assert nutrition.meal_status([]) == "missing"
    assert nutrition.meal_status(["无"]) == "skipped"
    assert nutrition.meal_status(["—"]) == "skipped"
    assert nutrition.meal_status(["没吃早餐，只喝了水"]) == "skipped"
    assert nutrition.meal_status(["无糖可乐"]) == "eaten"
    assert nutrition.meal_status(["没有胃口吃了半碗面"]) == "eaten"


def test_week_skipped_breakfast_plus_lunch_and_dinner_qualifies():
    monday = date(2026, 10, 26)
    entries = [
        meal(monday, "早餐", "没吃"),
        meal(monday, "午餐", "米饭和青菜"),
        meal(monday, "晚餐", "米饭和鸡蛋"),
    ]

    result = nutrition.summarize(entries, "week", monday, today=monday)

    assert result["complete"] is True
    assert result["missing"] == []
    analysis = result["analysis"]
    assert analysis is not None
    assert analysis["overview"]["qualified_days"] == 1
    assert analysis["overview"]["total_meals"] == 2
    assert analysis["overview"]["skipped_meals"] == 1
    assert any("1 餐标记为没吃" in item for item in analysis["observations"])


def test_week_skipped_breakfast_plus_one_meal_is_short_by_one():
    monday = date(2026, 10, 26)
    entries = [
        meal(monday, "早餐", "没吃"),
        meal(monday, "午餐", "米饭和青菜"),
    ]

    result = nutrition.summarize(entries, "week", monday, today=monday)

    assert result["complete"] is False
    assert result["analysis"] is None
    assert result["missing"] == [{
        "date": monday.isoformat(),
        "meals": 1,
        "needed": 2,
        "missing_meals": ["晚餐"],
        "shortfall": 1,
    }]


def test_week_day_with_two_meals_counts_as_complete():
    monday = date(2026, 10, 19)
    entries = [
        meal(monday, "早餐", "牛奶和全麦面包"),
        meal(monday, "午餐", "米饭、青菜和鸡胸肉"),
    ]

    result = nutrition.summarize(entries, "week", monday, today=monday)

    assert result["required_days"] == 1
    assert result["complete"] is True
    assert result["missing"] == []
    assert result["analysis"]["mode"] == "full_week"
    assert result["analysis"]["overview"]["total_meals"] == 2


def test_week_day_with_only_one_meal_blocks_analysis():
    monday = date(2026, 10, 19)
    result = nutrition.summarize(
        [meal(monday, "早餐", "牛奶和全麦面包")], "week", monday, today=monday
    )

    assert result["complete"] is False
    assert result["analysis"] is None
    assert result["missing"] == [{
        "date": monday.isoformat(),
        "meals": 1,
        "needed": 2,
        "missing_meals": ["午餐", "晚餐"],
        "shortfall": 1,
    }]


def test_week_current_week_only_requires_days_through_today():
    monday = date(2026, 10, 19)
    today = date(2026, 10, 21)
    entries = [
        meal(monday + timedelta(days=offset), meal_type, description)
        for offset in range(3)
        for meal_type, description in {
            "早餐": "牛奶和全麦面包",
            "午餐": "米饭、青菜和鸡胸肉",
        }.items()
    ]

    result = nutrition.summarize(entries, "week", monday, today=today)

    assert result["required_days"] == 3
    assert result["complete"] is True
    assert result["analysis"] is not None


def test_week_historical_week_requires_all_seven_days():
    monday = date(2026, 10, 5)
    today = date(2026, 10, 27)
    entries = [
        meal(monday + timedelta(days=offset), meal_type, description)
        for offset in range(6)
        for meal_type, description in {
            "早餐": "牛奶和全麦面包",
            "午餐": "米饭、青菜和鸡胸肉",
        }.items()
    ]

    result = nutrition.summarize(entries, "week", monday, today=today)

    assert result["required_days"] == 7
    assert result["complete"] is False
    assert result["analysis"] is None
    assert [item["date"] for item in result["missing"]] == [(monday + timedelta(days=6)).isoformat()]
    assert result["missing"][0]["shortfall"] == 2


def test_week_complete_analysis_has_overview_advantages_improvements_and_health_tips():
    monday = date(2026, 10, 5)
    today = date(2026, 10, 27)
    entries = [
        meal(monday + timedelta(days=offset), meal_type, description)
        for offset in range(7)
        for meal_type, description in {
            "早餐": "牛奶和全麦面包",
            "午餐": "米饭、青菜和鸡胸肉",
            "晚餐": "番茄豆腐汤和米饭",
        }.items()
    ]

    result = nutrition.summarize(entries, "week", monday, today=today)

    assert result["complete"] is True
    assert result["analysis"] is not None
    analysis = result["analysis"]
    assert analysis["mode"] == "full_week"
    assert analysis["overview"] == {
        "required_days": 7,
        "recorded_days": 7,
        "qualified_days": 7,
        "total_meals": 21,
        "min_meals_per_day": 2,
    }
    assert analysis["overview_text"]
    assert analysis["observations"]
    assert analysis["advantages"]
    assert any("水果" in item for item in analysis["improvements"])
    assert len(analysis["health_tips"]) >= 4
    assert any("饮水" in item for item in analysis["health_tips"])
    assert analysis["estimated_calories"] is None


def test_week_without_records_returns_daily_gaps():
    monday = date(2026, 10, 5)
    result = nutrition.summarize([], "week", monday, today=date(2026, 10, 27))

    assert result["complete"] is False
    assert result["analysis"] is None
    assert len(result["missing"]) == 7
    assert all(item["meals"] == 0 and item["shortfall"] == 2 for item in result["missing"])
    assert all(len(item["missing_meals"]) == 3 for item in result["missing"])
    assert result["reminders"] == []


def test_daily_skipped_lunch_or_dinner_reminds_to_eat():
    today = date.today()
    entries = [
        meal(today, "早餐", "没吃"),
        meal(today, "午餐", "没吃"),
        meal(today, "晚餐", "米饭和青菜"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["reminders"] == ["午餐没吃，记得好好吃饭哦。"]
    assert result["analysis"]["reminders"] == result["reminders"]


def test_daily_skipped_lunch_and_dinner_share_one_reminder():
    today = date.today()
    entries = [
        meal(today, "早餐", "牛奶和面包"),
        meal(today, "午餐", "没有吃"),
        meal(today, "晚餐", "跳过"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["reminders"] == ["午餐、晚餐没吃，记得好好吃饭哦。"]


def test_daily_unfilled_meal_is_not_treated_as_skipped():
    today = date.today()
    result = nutrition.summarize(
        [meal(today, "早餐", "牛奶和面包")], "day", today, today=today
    )

    assert result["reminders"] == []
    assert {item["meal_type"] for item in result["missing"]} == {"午餐", "晚餐"}


def test_daily_only_breakfast_skipped_does_not_remind():
    today = date.today()
    entries = [
        meal(today, "早餐", "没吃"),
        meal(today, "午餐", "米饭和青菜"),
        meal(today, "晚餐", "鸡蛋和豆腐"),
    ]

    result = nutrition.summarize(entries, "day", today, today=today)

    assert result["reminders"] == []
    assert result["analysis"]["skipped_meals"] == ["早餐"]


def test_week_three_skipped_main_meals_warns_about_nutrition():
    monday = date(2026, 10, 26)
    entries = []
    for offset, meal_type in ((0, "午餐"), (1, "晚餐"), (2, "午餐")):
        day = monday + timedelta(days=offset)
        entries.append(meal(day, "早餐", "牛奶和面包"))
        entries.append(meal(day, meal_type, "没吃"))
        other = "晚餐" if meal_type == "午餐" else "午餐"
        entries.append(meal(day, other, "米饭和青菜"))
    for offset in range(3, 7):
        day = monday + timedelta(days=offset)
        entries.append(meal(day, "午餐", "米饭和青菜"))
        entries.append(meal(day, "晚餐", "鸡蛋和豆腐"))

    result = nutrition.summarize(entries, "week", monday, today=date(2026, 11, 2))

    assert result["analysis"] is not None
    assert result["reminders"] == [
        "这周有 3 顿午餐或晚餐没吃，每周营养摄取量可能不足，记得好好吃饭哦。"
    ]


def test_week_two_skipped_main_meals_do_not_warn():
    monday = date(2026, 10, 26)
    entries = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        entries.append(meal(day, "午餐", "米饭和青菜"))
        entries.append(meal(day, "晚餐", "没吃" if offset < 2 else "鸡蛋和豆腐"))

    result = nutrition.summarize(entries, "week", monday, today=date(2026, 11, 2))

    assert result["reminders"] == []


def test_week_breakfast_skips_do_not_count_toward_three():
    monday = date(2026, 10, 26)
    entries = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        entries.append(meal(day, "早餐", "没吃"))
        entries.append(meal(day, "午餐", "米饭和青菜"))
        entries.append(meal(day, "晚餐", "鸡蛋和豆腐"))

    result = nutrition.summarize(entries, "week", monday, today=date(2026, 11, 2))

    assert result["reminders"] == []
    assert result["analysis"]["overview"]["skipped_meals"] == 7


def test_nutrition_summary_api_analyzes_partial_day(client):
    today = date.today()
    date_text = today.isoformat()
    client.post("/api/v1/meals", json={"description": "牛奶和面包", "meal_type": "早餐", "eaten_at": f"{date_text}T08:00:00"})

    response = client.get(f"/api/v1/nutrition-summary?period=day&date={date_text}")

    assert response.status_code == 200
    data = response.json()
    assert data["complete"] is False
    assert data["analysis"] is not None
    assert {item["meal_type"] for item in data["missing"]} == {"午餐", "晚餐"}
    assert {item["meal_type"] for item in data["analysis"]["missing_meals"]} == {"午餐", "晚餐"}
