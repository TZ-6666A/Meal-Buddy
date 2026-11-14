from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.accounts import hash_password
from app.config import PROJECT_ROOT, Settings
from app.db import get_db
from app.models import User
from app.security import client_host


def _request(peer: str, forwarded: str = "") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 1234),
        }
    )


def test_client_host_uses_peer_when_public():
    assert client_host(_request("1.2.3.4", "9.9.9.9")) == "1.2.3.4"


def test_client_host_reads_forwarded_for_behind_private_proxy():
    # Caddy/nginx 在宿主机或 Docker 网桥内，对端是内网地址时才采信转发头。
    assert client_host(_request("172.18.0.1", "223.104.11.159")) == "223.104.11.159"
    assert client_host(_request("127.0.0.1", "1.2.3.4, 172.18.0.1")) == "1.2.3.4"


def test_client_host_ignores_private_hops_in_forwarded_for():
    assert client_host(_request("127.0.0.1", "10.0.0.5, 192.168.1.7")) == "127.0.0.1"


def test_client_host_falls_back_to_peer_without_headers():
    assert client_host(_request("127.0.0.1")) == "127.0.0.1"


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


def _seed_admin(client, username="admin", password="admin-pass-123"):
    with client.testing_session() as session:
        user = User(username=username, password_hash=hash_password(password), is_admin=True)
        session.add(user)
        session.commit()
        return user.id


def test_token_header_maps_to_admin_account(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="test-secret", is_production=False, dev_auto_login=False),
    )
    _seed_admin(client)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok", "service": "fandazi-api", "database": "ok"
    }
    assert client.get("/api/v1/meals").status_code == 401
    assert client.get("/api/v1/meals", headers={"X-API-Token": "test-secret"}).status_code == 200


def test_wrong_token_rejected_even_in_dev(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="test-secret", is_production=False, dev_auto_login=True),
    )
    # 配了 API_TOKEN 就不再走开发免登录旁路，乱给的 token 一律 401。
    assert client.get("/api/v1/meals", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/api/v1/meals").status_code == 401


def test_sensitive_endpoints_require_login_when_token_configured(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(
            api_token="s3cret-token-value", is_production=True, dev_auto_login=False
        ),
    )

    assert client.get("/api/v1/export").status_code == 401
    assert client.post("/api/v1/maintenance/prune", json={"days": 90}).status_code == 401
    assert client.request("DELETE", "/api/v1/data", json={"confirm": "DELETE"}).status_code == 401

    _seed_admin(client)
    allowed = client.get(
        "/api/v1/export", headers={"X-API-Token": "s3cret-token-value"}
    )
    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "no-store"


def test_no_auth_when_dev_auto_login_disabled(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="", is_production=False, dev_auto_login=False),
    )
    assert client.get("/api/v1/export").status_code == 401
    assert client.get("/api/v1/meals").status_code == 401


def test_dev_auto_login_grants_admin_access(client, monkeypatch):
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="", is_production=False, dev_auto_login=True),
    )
    assert client.get("/api/v1/export").status_code == 200
    # dev 用户是管理员，可以执行全站清理。
    assert client.post("/api/v1/maintenance/prune", json={"days": 90}).status_code == 200


def test_settings_anchor_env_file_to_project_root():
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"


def test_production_startup_requires_api_token():
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        Settings(_env_file=None, app_env="production", api_token="").validate_for_startup()

    with pytest.raises(RuntimeError, match="16"):
        Settings(_env_file=None, app_env="production", api_token="short").validate_for_startup()

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        Settings(
            _env_file=None,
            app_env="production",
            api_token="a" * 20,
            admin_password="",
            database_url="postgresql+psycopg://user:pw@127.0.0.1:5433/fandazi",
        ).validate_for_startup()

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        Settings(
            _env_file=None,
            app_env="production",
            api_token="a" * 20,
            admin_password="short",
            database_url="postgresql+psycopg://user:pw@127.0.0.1:5433/fandazi",
        ).validate_for_startup()

    Settings(
        _env_file=None,
        app_env="production",
        api_token="a" * 20,
        admin_password="long-enough-pass",
        database_url="postgresql+psycopg://user:pw@127.0.0.1:5433/fandazi",
    ).validate_for_startup()
