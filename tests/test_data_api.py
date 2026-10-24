from __future__ import annotations


def test_export_returns_all_sections(client):
    client.post("/api/v1/meals", json={"description": "番茄炒蛋"})
    client.post("/api/v1/pantry", json={"name": "番茄"})
    client.post("/api/v1/preferences", json={"kind": "like", "topic": "土豆", "text": "爱吃土豆"})

    data = client.get("/api/v1/export").json()

    assert data["meals"]
    assert data["pantry"]
    assert data["preferences"]
    assert "exported_at" in data


def test_delete_requires_confirm(client):
    response = client.request("DELETE", "/api/v1/data", json={"confirm": "no"})

    assert response.status_code == 400


def test_delete_clears_everything(client):
    client.post("/api/v1/meals", json={"description": "番茄炒蛋"})
    client.post("/api/v1/pantry", json={"name": "番茄"})

    response = client.request("DELETE", "/api/v1/data", json={"confirm": "DELETE"})

    assert response.status_code == 200
    assert response.json()["deleted"]["meal_entries"] == 1
    data = client.get("/api/v1/export").json()
    assert data["meals"] == []
    assert data["pantry"] == []


def test_usage_endpoint_shape(client):
    data = client.get("/api/v1/usage").json()

    assert "total_tokens" in data
    assert "calls" in data
