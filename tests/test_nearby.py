import pytest
from types import SimpleNamespace

from app.services import nearby
from app.services.nearby import NearbyError, choose, fetch_location_label, fetch_places, parse_places


SAMPLE = {
    "status": "1",
    "pois": [
        {
            "name": "老张烤鱼",
            "distance": "600",
            "business": {"tag": "烤鱼,麻辣香锅,美食"},
        },
        {
            "name": "甜品铺",
            "distance": "200",
            "business": {"tag": "双皮奶,芒果布丁"},
        },
        {
            "name": "空店",
            "distance": "100",
            "business": {"tag": "美食,餐厅"},
        },
    ],
}

NEARBY_MATCHES = [
    {"shop": "秦风外卖店", "dish": "麻辣鸡肉盖饭", "meters": 350, "source": "nearby"},
]


def test_parse_places_keeps_signature_dishes_and_drops_generic_tags():
    places = parse_places(SAMPLE)

    assert [item["dish"] for item in places] == ["双皮奶", "芒果布丁", "烤鱼", "麻辣香锅"]
    assert places[0]["shop"] == "甜品铺"
    assert places[0]["meters"] == 200


def test_choose_matches_taste_and_skips_blocked_dish():
    places = parse_places(SAMPLE)

    spicy = choose(places, {"taste": "spicy", "staple": "any"}, [], [])
    sweet = choose(places, {"taste": "sweet", "staple": "any"}, ["芒果"], [])

    assert spicy["choice"] == "附近老张烤鱼的麻辣香锅"
    assert sweet["choice"] == "附近甜品铺的双皮奶"
    assert "约 200 米" in sweet["reasons"]


def test_fetch_places_explains_how_to_configure_missing_key():
    with pytest.raises(NearbyError, match=r"\.env.*AMAP_KEY.*重启后端"):
        fetch_places(121.4737, 31.2304, "")


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


def test_fetch_city_places_searches_within_the_detected_city(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return SAMPLE

    def fake_get(url, params, timeout):
        assert url == nearby.TEXT_URL
        assert params["city"] == "西安市"
        assert params["citylimit"] == "true"
        assert params["keywords"] == "特色美食"
        return Response()

    monkeypatch.setattr(nearby.httpx, "get", fake_get)

    assert nearby.fetch_city_places("西安市", "test-key")[0]["dish"] == "双皮奶"


def test_location_api_returns_and_persists_city_district(client, monkeypatch):
    monkeypatch.setattr(nearby, "fetch_places", lambda lng, lat, key: [])
    def city_search(city, key):
        assert city == "商洛市"
        return NEARBY_MATCHES
    monkeypatch.setattr(nearby, "fetch_city_places", city_search)
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
    for option in ("spicy", "rice", "heavy"):
        response = client.post(f"/api/v1/decisions/{session_id}/answer?option={option}")
        assert response.status_code == 200

    response = client.post(
        f"/api/v1/decisions/{session_id}/location",
        json={"lat": 30.2741, "lng": 120.1551},
    )

    assert response.status_code == 200
    assert response.json()["location_label"] == "商洛市 · 商州区"
    assert response.json()["location_data_version"] == nearby.REGIONAL_CATALOG_VERSION
    assert "商洛市特色：" in response.json()["result"]
    assert response.json()["available_sources"] == ["local", "nearby"]
    assert client.get(f"/api/v1/decisions/{session_id}").json()["location_label"] == "商洛市 · 商州区"

    switched = client.post(f"/api/v1/decisions/{session_id}/source?source=nearby")
    assert switched.status_code == 200
    assert switched.json()["recommendation_source"] == "nearby"
    assert "附近秦风外卖店的麻辣鸡肉盖饭" in switched.json()["result"]

    switched_back = client.post(f"/api/v1/decisions/{session_id}/source?source=local")
    assert switched_back.status_code == 200
    assert switched_back.json()["recommendation_source"] == "local"
    assert "商洛市特色" in switched_back.json()["result"]


def test_regional_specialties_are_filtered_by_taste_and_staple():
    places = nearby.regional_places("商洛市 · 商州区")

    spicy = nearby.choose(places, {"taste": "spicy", "staple": "any"}, [], [])
    noodle = nearby.choose(places, {"taste": "light", "staple": "noodle"}, [], [])
    sweet = nearby.choose(places, {"taste": "sweet", "staple": "any"}, [], [])

    assert spicy["choice"] == "商洛市特色：商洛洋芋糍粑"
    assert noodle["choice"] == "商洛市特色：商洛浆水面"
    assert sweet["choice"] == "商洛市特色：商洛柿饼"


def test_sources_are_filtered_separately_by_the_same_answers():
    places = nearby.regional_places("商洛市 · 商州区") + NEARBY_MATCHES
    answers = {"taste": "spicy", "staple": "rice", "heaviness": "heavy"}

    assert nearby.sources_with_matches(places, answers, []) == ["local", "nearby"]
    assert nearby.choose(places, answers, [], [], source="local")["choice"] == "商洛市特色：镇安腊肉炒土豆"
    assert nearby.choose(places, answers, [], [], source="nearby")["choice"] == "附近秦风外卖店的麻辣鸡肉盖饭"
    assert nearby.alternatives(places, answers, [], [], source="nearby") == [
        "附近秦风外卖店的麻辣鸡肉盖饭"
    ]
