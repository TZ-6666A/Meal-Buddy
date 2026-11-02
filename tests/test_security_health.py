from types import SimpleNamespace

import pytest

from app.config import Settings
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


def test_sensitive_endpoints_require_token_when_configured(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="s3cret-token-value", is_production=True),
    )

    assert client.get("/api/v1/export").status_code == 401
    assert client.post("/api/v1/maintenance/prune", json={"days": 90}).status_code == 401
    assert client.request("DELETE", "/api/v1/data", json={"confirm": "DELETE"}).status_code == 401

    allowed = client.get(
        "/api/v1/export", headers={"X-API-Token": "s3cret-token-value"}
    )
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"


def test_sensitive_endpoints_deny_remote_callers_when_token_missing(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="", is_production=False),
    )
    monkeypatch.setattr("app.security.client_host", lambda request: "192.168.1.20")

    assert client.get("/api/v1/export").status_code == 403
    assert client.post("/api/v1/maintenance/prune", json={"days": 90}).status_code == 403
    # 普通接口不受影响，本地开发仍然可用。
    assert client.get("/api/v1/meals").status_code == 200


def test_sensitive_endpoints_allow_loopback_when_token_missing(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="", is_production=False),
    )
    monkeypatch.setattr("app.security.client_host", lambda request: "127.0.0.1")

    assert client.get("/api/v1/export").status_code == 200


def test_production_startup_requires_api_token():
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        Settings(_env_file=None, app_env="production", api_token="").validate_for_startup()

    with pytest.raises(RuntimeError, match="16"):
        Settings(_env_file=None, app_env="production", api_token="short").validate_for_startup()

    Settings(
        _env_file=None,
        app_env="production",
        api_token="a" * 20,
        database_url="postgresql+psycopg://user:pw@127.0.0.1:5433/fandazi",
    ).validate_for_startup()
