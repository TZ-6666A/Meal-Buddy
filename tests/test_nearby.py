from types import SimpleNamespace

from app.services import nearby
from app.services.nearby import choose, fetch_location_label
from app.api import _no_match_suggestion


def test_fetch_location_label_uses_reverse_geocode(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": "1",
                "regeocode": {
                    "addressComponent": {
                        "province": "浙江省",
                        "city": "杭州市",
                        "district": "西湖区",
                    }
                },
            }

    def fake_get(url, params, timeout):
        assert url == nearby.REGEOCODE_URL
        assert params["location"] == "120.155100,30.274100"
        assert params["extensions"] == "base"
        assert params["key"] == "test-key"
        return Response()

    monkeypatch.setattr(nearby.httpx, "get", fake_get)

    assert fetch_location_label(120.1551, 30.2741, "test-key") == "杭州市 · 西湖区"


def test_location_api_returns_and_persists_city_district(client, monkeypatch):
    monkeypatch.setattr(
        nearby,
        "fetch_location_label",
        lambda lng, lat, key: "商洛市 · 商州区",
    )
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: SimpleNamespace(daily_token_budget=0, amap_key="test-key"),
    )

    started = client.post("/api/v1/decisions?mode=takeout")
    session_id = started.json()["session_id"]
    for option in ("sweet", "rice", "heavy"):
        response = client.post(f"/api/v1/decisions/{session_id}/answer?option={option}")
        assert response.status_code == 200

    response = client.post(
        f"/api/v1/decisions/{session_id}/location",
        json={"lat": 30.2741, "lng": 120.1551},
    )

    assert response.status_code == 200
    assert response.json()["location_label"] == "商洛市 · 商州区"
    assert response.json()["location_data_version"] == nearby.REGIONAL_CATALOG_VERSION
    # 菜库会持续扩充，断言跟着候选池动态算，避免新菜入池后随机挂
    from app.services import decision
    answers = {"taste": "sweet", "staple": "rice", "heaviness": "heavy"}
    ranking = decision.ranked("takeout", answers, "", [], [])
    best = ranking[0][1]
    band = {name for name, score in ranking if score >= best - 1}
    assert decision.draw("takeout", answers, "", "", []) in band
    assert any(dish in response.json()["result"] for dish in band)
    assert "附近" not in response.json()["result"]
    assert client.get(f"/api/v1/decisions/{session_id}").json()["location_label"] == "商洛市 · 商州区"


def test_regional_specialties_are_filtered_by_taste_and_staple():
    places = nearby.regional_places("商洛市 · 商州区")

    spicy = nearby.choose(places, {"taste": "spicy", "staple": "any"}, [], [])
    noodle = nearby.choose(places, {"taste": "light", "staple": "noodle"}, [], [])
    sweet = nearby.choose(places, {"taste": "sweet", "staple": "any"}, [], [])

    assert spicy["choice"] == "商洛洋芋糍粑"
    assert noodle["choice"] == "商洛浆水面"
    assert sweet["choice"] == "商洛柿饼"


def test_local_dishes_keep_plain_names_and_ignore_restaurant_candidates():
    places = nearby.regional_places("商洛市 · 商州区")
    answers = {"taste": "spicy", "staple": "rice", "heaviness": "heavy"}

    assert nearby.choose(places, answers, [], [], source="local")["choice"] == "镇安腊肉炒土豆"
    assert nearby.alternatives(places, answers, [], [], source="local") == [
        "镇安腊肉炒土豆", "商州烩菜配米饭"
    ]


def test_no_match_suggestion_explains_selected_conditions():
    message = _no_match_suggestion(
        "商洛市 · 商州区",
        {"taste": "sweet", "staple": "rice", "heaviness": "heavy"},
        True,
    )

    assert "商洛市 · 商州区" in message
    assert "甜口、米饭、丰盛" in message
    assert "常见菜" in message


def test_ai_city_dishes_enter_recommendation(client, monkeypatch):
    """AI 整理的"当地人常吃"菜品应参与推荐；本地特产缺席的城市也能命中。"""
    from types import SimpleNamespace as NS

    from app.services import city_dishes

    monkeypatch.setattr(
        nearby, "fetch_location_label", lambda lng, lat, key: "北京市 · 朝阳区"
    )
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: NS(daily_token_budget=0, amap_key="test-key"),
    )
    monkeypatch.setattr(
        city_dishes, "prefetch", lambda city, settings=None: None
    )
    monkeypatch.setattr(
        city_dishes, "cached",
        lambda city: [{
            "dish": "老北京炸酱面", "source": "ai-city", "region": "北京市",
            "tastes": ("salty",), "staples": ("noodle",), "heaviness": None,
        }],
    )
    started = client.post("/api/v1/decisions?mode=takeout")
    session_id = started.json()["session_id"]
    for option in ("salty", "noodle", "heavy"):
        assert client.post(f"/api/v1/decisions/{session_id}/answer?option={option}").status_code == 200

    response = client.post(
        f"/api/v1/decisions/{session_id}/location",
        json={"lat": 39.9042, "lng": 116.4074},
    )
    assert response.status_code == 200
    body = response.json()
    assert "老北京炸酱面" in body["result"]
    assert "北京市当地人常吃" in " ".join(body["reasons"])


def test_locate_by_ip_skips_private_addresses():
    assert nearby.locate_by_ip("172.17.0.1", "test-key") == ""
    assert nearby.locate_by_ip("192.168.1.10", "test-key") == ""
    assert nearby.locate_by_ip("127.0.0.1", "test-key") == ""
    assert nearby.locate_by_ip("unknown", "test-key") == ""
    assert nearby.locate_by_ip("1.2.3.4", "") == ""


def test_locate_by_ip_returns_city(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "1", "province": "陕西省", "city": "商洛市"}

    def fake_get(url, params, timeout):
        assert url == nearby.IP_LOCATE_URL
        assert params["ip"] == "1.2.3.4"
        assert params["key"] == "test-key"
        return Response()

    monkeypatch.setattr(nearby.httpx, "get", fake_get)

    assert nearby.locate_by_ip("1.2.3.4", "test-key") == "商洛市"


def test_city_label_normalizes_name():
    assert nearby.city_label("商洛") == "商洛市"
    assert nearby.city_label("商洛市") == "商洛市"
    assert nearby.city_label("") == ""
    assert nearby.city_label("  ") == ""


def test_ip_location_api_returns_empty_for_private_client(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: SimpleNamespace(daily_token_budget=0, amap_key="test-key"),
    )

    response = client.get("/api/v1/location/ip")

    assert response.status_code == 200
    # 测试客户端走 127.0.0.1，属于内网地址，应返回空城市
    assert response.json() == {"city": "", "source": ""}


def test_save_city_api_sets_location_label(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: SimpleNamespace(daily_token_budget=0, amap_key="test-key"),
    )
    monkeypatch.setattr("app.services.city_dishes.cached", lambda label: [])
    monkeypatch.setattr("app.services.city_dishes.prefetch", lambda label: None)

    started = client.post("/api/v1/decisions?mode=takeout")
    session_id = started.json()["session_id"]

    response = client.post(
        f"/api/v1/decisions/{session_id}/city",
        json={"city": "商洛"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["location_label"] == "商洛市"
    assert body["location_data_version"] == nearby.REGIONAL_CATALOG_VERSION
    assert client.get(f"/api/v1/decisions/{session_id}").json()["location_label"] == "商洛市"


def test_save_city_api_rejects_blank_city(client):
    started = client.post("/api/v1/decisions?mode=takeout")
    session_id = started.json()["session_id"]

    response = client.post(f"/api/v1/decisions/{session_id}/city", json={"city": ""})

    assert response.status_code == 422
