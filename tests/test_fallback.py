from __future__ import annotations

import asyncio

from app.schemas import Recipe, RecipeIngredient, RecommendationRequest
from app.services.decision import expand_blocked, is_blocked, prefer_ingredients
from app.services.meal_assistant import DietAgent


def test_recommend_falls_back_to_demo_when_model_fails(client, fake_settings, monkeypatch):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(DietAgent, "_request_recipes", boom)

    response = client.post(
        "/api/v1/recommendations",
        json={"ingredients": ["番茄", "鸡蛋", "豆腐"], "available_minutes": 30},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "fallback"
    assert data["recipes"]
    assert "南瓜小米粥" not in {recipe["name"] for recipe in data["recipes"]}
    assert all(
        DietAgent._recipe_matches_available_ingredients(
            Recipe.model_validate(recipe), ["番茄", "鸡蛋", "豆腐"]
        )
        for recipe in data["recipes"]
    )
    assert data["prompt_version"]
    assert data["degraded"] is True
    assert data["model"] == ""
    assert data["duration_ms"] >= 0


def test_recommend_returns_422_when_nothing_fits_allergies(client, fake_settings, monkeypatch):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(DietAgent, "_request_recipes", boom)
    monkeypatch.setattr(DietAgent, "_demo", staticmethod(lambda *args, **kwargs: ("", [], "demo")))
    monkeypatch.setattr(DietAgent, "_local_recipe", staticmethod(lambda *args, **kwargs: None))
    client.post("/api/v1/preferences", json={"kind": "allergy", "topic": "鸡蛋", "text": "对鸡蛋过敏"})

    response = client.post("/api/v1/recommendations", json={"available_minutes": 30})

    assert response.status_code == 422
    assert "忌口" in response.json()["detail"]


def test_egg_allergy_blocks_dishes_with_egg_in_the_name():
    blocked = expand_blocked(["鸡蛋"])

    assert is_blocked("番茄炒蛋", blocked)
    assert is_blocked("虾仁蒸蛋", blocked)
    assert not is_blocked("青椒肉丝", blocked)


def test_prefer_ingredients_keeps_all_candidates_but_puts_matches_first():
    ranking = [("土豆炖鸡", 9), ("番茄炒蛋", 5), ("青椒肉丝", 3)]

    ordered = prefer_ingredients(ranking, ["番茄"])

    assert ordered[0] == ("番茄炒蛋", 5)
    assert len(ordered) == 3
    assert set(ordered) == set(ranking)


def test_fallback_never_returns_blocked_recipes(client, fake_settings, monkeypatch):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(DietAgent, "_request_recipes", boom)
    client.post("/api/v1/preferences", json={"kind": "allergy", "topic": "鸡蛋", "text": "对鸡蛋过敏"})

    response = client.post(
        "/api/v1/recommendations",
        json={"ingredients": [], "available_minutes": 30},
    )

    assert response.status_code == 200
    assert len(response.json()["recipes"]) >= 12
    for recipe in response.json()["recipes"]:
        texts = [recipe["name"]] + [i["name"] for i in recipe["ingredients"]]
        assert not any("鸡蛋" in text for text in texts)


def test_model_recommendation_keeps_a_twelve_recipe_pool(fake_settings, monkeypatch):
    recipes = [
        Recipe(
            name=f"家常菜{i}",
            description="快手家常菜",
            prep_minutes=15,
            ingredients=[{"name": "番茄", "amount": "1 个"}],
            steps=["洗净切好", "炒熟出锅"],
            tags=[],
        )
        for i in range(12)
    ]

    async def fake_recipes(self, *args, **kwargs):
        return "好的", recipes

    monkeypatch.setattr(DietAgent, "_request_recipes", fake_recipes)

    _, result, source = asyncio.run(
        DietAgent().recommend(RecommendationRequest(), [], [], [], [])
    )

    assert source == "llm"
    assert len(result) == 12


def test_short_model_recommendation_is_padded_to_twelve(fake_settings, monkeypatch):
    recipe = Recipe(
        name="番茄炒菜",
        description="快手家常菜",
        prep_minutes=15,
        ingredients=[{"name": "番茄", "amount": "1 个"}],
        steps=["洗净切好", "炒熟出锅"],
        tags=[],
    )

    async def fake_recipes(self, *args, **kwargs):
        return "好的", [recipe]

    monkeypatch.setattr(DietAgent, "_request_recipes", fake_recipes)

    _, result, _source = asyncio.run(
        DietAgent().recommend(RecommendationRequest(), [], [], [], [])
    )

    assert len(result) >= 12


def test_short_time_fallback_keeps_twelve_safe_quick_recipes():
    request = RecommendationRequest(available_minutes=15)

    _, recipes, _ = DietAgent._demo(request, [], ["鸡蛋"])

    assert len(recipes) >= 12
    assert all(recipe.prep_minutes <= request.available_minutes for recipe in recipes)
    assert all(not DietAgent._recipe_blocked(recipe, ["鸡蛋"]) for recipe in recipes)


def test_demo_filters_recipes_to_selected_ingredients():
    request = RecommendationRequest(
        ingredients=["豆腐", "鸡蛋", "番茄"], available_minutes=30
    )

    _, recipes, _ = DietAgent._demo(request, [])

    names = {recipe.name for recipe in recipes}
    assert "番茄鸡蛋豆腐汤" in names
    assert "番茄豆腐汤" in names
    assert "番茄豆腐蒸蛋" in names
    assert "无油番茄豆腐炒蛋" in names
    assert "番茄豆腐冷盘" in names
    assert "南瓜小米粥" not in names
    assert all(
        DietAgent._recipe_matches_available_ingredients(
            recipe, request.ingredients
        )
        for recipe in recipes
    )


def test_inventory_recipe_pool_includes_and_spreads_multiple_cooking_styles(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.services.meal_assistant.get_settings",
        lambda: SimpleNamespace(openai_api_key="", openai_model="demo"),
    )
    request = RecommendationRequest(
        ingredients=["番茄", "鸡蛋", "豆腐"], available_minutes=30
    )

    _, recipes, _source = asyncio.run(DietAgent().recommend(request, [], [], [], []))

    styles = [DietAgent._recipe_style(recipe) for recipe in recipes]
    assert {"soup", "steam", "stir_fry", "cold"} <= set(styles)
    assert styles[0] != styles[1]


def test_oil_free_water_stir_fry_is_allowed_without_oil():
    recipe = Recipe(
        name="无油番茄豆腐炒蛋",
        description="用清水代替油",
        prep_minutes=20,
        ingredients=[
            {"name": "番茄", "amount": "1 个"},
            {"name": "嫩豆腐", "amount": "半盒"},
            {"name": "鸡蛋", "amount": "1 个"},
            {"name": "清水", "amount": "少量"},
        ],
        steps=["加清水翻炒至熟"],
        tags=["炒菜", "无油水炒"],
    )

    assert DietAgent._recipe_matches_available_ingredients(
        recipe, ["番茄", "豆腐", "鸡蛋"]
    )


def test_ingredient_match_accepts_common_names_and_water_only():
    recipe = Recipe(
        name="西红柿嫩豆腐汤",
        description="",
        prep_minutes=15,
        ingredients=[
            {"name": "西红柿", "amount": "1 个"},
            {"name": "嫩豆腐", "amount": "半盒"},
            {"name": "清水", "amount": "适量"},
        ],
        steps=["煮熟"],
    )

    assert DietAgent._recipe_matches_available_ingredients(
        recipe, ["2个番茄", "半盒豆腐"]
    )

    recipe.ingredients.append(RecipeIngredient(name="食用油", amount="少许"))
    assert not DietAgent._recipe_matches_available_ingredients(
        recipe, ["番茄", "豆腐"]
    )


def test_ingredient_match_rejects_unlisted_oil_in_steps():
    recipe = Recipe(
        name="番茄鸡蛋汤",
        description="",
        prep_minutes=15,
        ingredients=[
            {"name": "番茄", "amount": "1 个"},
            {"name": "鸡蛋", "amount": "1 个"},
        ],
        steps=["热锅加油，放入番茄翻炒。", "加水煮开后放入鸡蛋。"],
    )

    assert not DietAgent._recipe_matches_available_ingredients(
        recipe, ["番茄", "鸡蛋"]
    )


def test_model_pool_discards_recipes_requiring_unavailable_ingredients(fake_settings, monkeypatch):
    recipes = [
        Recipe(
            name="南瓜小米粥",
            description="",
            prep_minutes=30,
            ingredients=[
                {"name": "南瓜", "amount": "150 克"},
                {"name": "小米", "amount": "半杯"},
            ],
            steps=["煮粥"],
        ),
        Recipe(
            name="番茄豆腐汤",
            description="",
            prep_minutes=15,
            ingredients=[
                {"name": "番茄", "amount": "1 个"},
                {"name": "嫩豆腐", "amount": "半盒"},
            ],
            steps=["煮汤"],
        ),
    ]

    async def fake_recipes(self, *args, **kwargs):
        return "优先推荐南瓜小米粥。", recipes

    monkeypatch.setattr(DietAgent, "_request_recipes", fake_recipes)
    request = RecommendationRequest(
        ingredients=["番茄", "鸡蛋", "豆腐"], available_minutes=30
    )

    explanation, result, _source = asyncio.run(
        DietAgent().recommend(request, [], [], [], [])
    )

    assert result
    assert "南瓜小米粥" not in {recipe.name for recipe in result}
    assert "南瓜" not in explanation
    assert all(
        DietAgent._recipe_matches_available_ingredients(recipe, request.ingredients)
        for recipe in result
    )


def test_demo_does_not_fill_with_unrelated_recipes_when_no_match_exists():
    request = RecommendationRequest(ingredients=["紫菜"], available_minutes=30)

    explanation, recipes, _ = DietAgent._demo(request, [])

    assert recipes == []
    assert "现有食材" in explanation


def test_available_ingredients_uses_selected_list_and_ignores_blank_entries():
    request = RecommendationRequest(ingredients=[" ", "", "番茄、豆腐"])

    _, recipes, _ = DietAgent._demo(request, ["南瓜", "小米"])

    assert recipes
    assert "南瓜小米粥" not in {recipe.name for recipe in recipes}
    assert all(
        DietAgent._recipe_matches_available_ingredients(recipe, ["番茄", "豆腐"])
        for recipe in recipes
    )


def test_blank_ingredients_do_not_activate_inventory_filter():
    request = RecommendationRequest(ingredients=[" ", ""], available_minutes=30)

    _, recipes, _ = DietAgent._demo(request, [])

    assert recipes
    assert "南瓜小米粥" in {recipe.name for recipe in recipes}


def test_saved_pantry_is_used_when_no_ingredients_are_selected():
    request = RecommendationRequest(available_minutes=30)

    _, recipes, _ = DietAgent._demo(request, ["番茄", "鸡蛋", "豆腐"])

    assert recipes
    assert "南瓜小米粥" not in {recipe.name for recipe in recipes}
    assert all(
        DietAgent._recipe_matches_available_ingredients(
            recipe, ["番茄", "鸡蛋", "豆腐"]
        )
        for recipe in recipes
    )


def test_api_explains_when_no_recipe_matches_available_ingredients(client):
    response = client.post(
        "/api/v1/recommendations",
        json={"ingredients": ["紫菜"], "available_minutes": 30},
    )

    assert response.status_code == 422
    assert "现有食材" in response.json()["detail"]


def test_stream_reports_no_recipe_when_inventory_has_no_match(client):
    from app.rate_limit import reset_rate_limits
    reset_rate_limits()
    with client.stream(
        "POST",
        "/api/v1/recommendations/stream",
        json={"ingredients": ["紫菜"], "available_minutes": 30},
    ) as response:
        body = "".join(response.iter_text())

    assert '"stage": "error"' in body
    assert "现有食材" in body
    assert '"stage": "done"' not in body


def test_model_recipes_over_time_limit_are_filtered_and_replaced(fake_settings, monkeypatch):
    recipe = Recipe(
        name="慢炖牛腩",
        description="需要更久",
        prep_minutes=35,
        ingredients=[{"name": "牛腩", "amount": "200 克"}],
        steps=["慢炖"],
        tags=[],
    )

    async def fake_recipes(self, *args, **kwargs):
        return "好的", [recipe]

    monkeypatch.setattr(DietAgent, "_request_recipes", fake_recipes)

    _, recipes, _ = asyncio.run(
        DietAgent().recommend(
            RecommendationRequest(available_minutes=30), [], [], [], []
        )
    )

    assert recipes
    assert all(recipe.prep_minutes <= 30 for recipe in recipes)


def test_long_time_prefers_matching_stir_fry_then_soup():
    recipes = [
        Recipe(name="快炒青菜", description="", prep_minutes=15,
               ingredients=[], steps=["炒熟"], tags=["炒菜"]),
        Recipe(name="菌菇鸡汤", description="", prep_minutes=40,
               ingredients=[], steps=["炖煮"], tags=["汤菜"]),
        Recipe(name="干煸豆角", description="", prep_minutes=35,
               ingredients=[], steps=["炒熟"], tags=["炒菜"]),
    ]

    ordered = DietAgent._rank_recipes_for_time(40, recipes)

    assert [recipe.name for recipe in ordered] == ["干煸豆角", "菌菇鸡汤", "快炒青菜"]


def test_long_time_falls_back_to_matching_soup_when_no_stir_fry():
    recipes = [
        Recipe(name="清蒸鲈鱼", description="", prep_minutes=30,
               ingredients=[], steps=["蒸熟"], tags=["蒸煮"]),
        Recipe(name="菌菇鸡汤", description="", prep_minutes=40,
               ingredients=[], steps=["炖煮"], tags=["汤菜"]),
        Recipe(name="紫菜蛋花汤", description="", prep_minutes=15,
               ingredients=[], steps=["煮开"], tags=["汤菜"]),
    ]

    ordered = DietAgent._rank_recipes_for_time(40, recipes)

    assert [recipe.name for recipe in ordered] == ["菌菇鸡汤", "清蒸鲈鱼", "紫菜蛋花汤"]


def test_short_time_prioritizes_recipes_close_to_selected_time():
    recipes = [
        Recipe(name="快炒青菜", description="", prep_minutes=15,
               ingredients=[], steps=["炒熟"], tags=[]),
        Recipe(name="番茄鸡蛋豆腐煲", description="", prep_minutes=20,
               ingredients=[], steps=["煮熟"], tags=[]),
    ]

    ordered = DietAgent._rank_recipes_for_time(20, recipes)

    assert [recipe.name for recipe in ordered] == ["番茄鸡蛋豆腐煲", "快炒青菜"]


def test_recommend_stream_emits_stages(client, fake_settings, monkeypatch):
    from app.rate_limit import reset_rate_limits
    reset_rate_limits()
    async def fake_recipes(self, *args, **kwargs):
        return "好的", [
            Recipe(
                name="番茄炒蛋",
                description="快手",
                prep_minutes=25,
                ingredients=[
                    {"name": "番茄", "amount": "1 个"},
                    {"name": "鸡蛋", "amount": "1 个"},
                    {"name": "食用油", "amount": "少许"},
                ],
                steps=["热锅加油，放入番茄和鸡蛋炒熟。"],
                tags=[],
            )
        ]

    monkeypatch.setattr(DietAgent, "_request_recipes", fake_recipes)

    with client.stream(
        "POST", "/api/v1/recommendations/stream",
        json={"ingredients": ["番茄", "鸡蛋", "食用油"], "available_minutes": 30},
    ) as response:
        body = "".join(response.iter_text())

    assert '"stage": "accepted"' in body
    assert '"stage": "thinking"' in body
    assert '"stage": "done"' in body
    assert "番茄炒蛋" in body
