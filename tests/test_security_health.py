from types import SimpleNamespace

from app.db import get_db


def test_health_reports_database_failure_as_503(client):
    def failing_db():
        class BrokenSession:
            def execute(self, statement):
                raise RuntimeError("offline")

        yield BrokenSession()

    client.app.dependency_overrides[get_db] = failing_db
    response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["detail"] == "数据库不可用"


def test_health_and_token_protected_meals(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings", lambda: SimpleNamespace(api_token="test-secret")
    )

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok", "service": "fandazi-api", "database": "ok"
    }
    assert client.get("/api/v1/meals").status_code == 401
    assert client.get("/api/v1/meals", headers={"X-API-Token": "test-secret"}).status_code == 200
