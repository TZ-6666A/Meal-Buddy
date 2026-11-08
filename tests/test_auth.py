"""登录系统测试：注册（邀请码）、登录、会话、数据隔离、管理员接口。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.accounts import hash_token
from app.main import app
from app.models import User, UserSession

INVITE = "LETMEIN"


@pytest.fixture()
def auth_env(client, monkeypatch):
    """开启邀请码、关闭开发免登录；返回可变的邀请码状态供测试调整。

    依赖 client：让本 fixture 的补丁在 client 的补丁之后生效。
    """
    state = {"invite_code": INVITE}

    def security_settings():
        return SimpleNamespace(
            api_token="",
            is_production=False,
            admin_username="admin",
            admin_password="",
            invite_code=state["invite_code"],
            session_ttl_days=30,
            dev_auto_login=False,
        )

    def api_settings():
        return SimpleNamespace(daily_token_budget=0, invite_code=state["invite_code"])

    monkeypatch.setattr("app.security.get_settings", security_settings)
    monkeypatch.setattr("app.api.get_settings", api_settings)
    # 登录/注册限流直接关闭，避免用例间互相干扰
    monkeypatch.setattr(
        "app.rate_limit.get_settings",
        lambda: SimpleNamespace(rate_limit_per_minute=10, auth_rate_limit_per_minute=0),
    )
    return state


def _register(client, username="xiaofan", password="password123", invite=INVITE, display_name=None):
    body = {"username": username, "password": password, "invite_code": invite}
    if display_name:
        body["display_name"] = display_name
    return client.post("/api/v1/auth/register", json=body)


def _second_client() -> TestClient:
    """共享依赖覆盖、独立 Cookie 罐的第二个客户端，用来模拟另一个用户。"""
    return TestClient(app)


def test_register_requires_open_invite(auth_env, client):
    response = _register(client, invite="WRONG")
    assert response.status_code == 403

    auth_env["invite_code"] = ""
    response = _register(client)
    assert response.status_code == 403

    auth_env["invite_code"] = INVITE
    response = _register(client)
    assert response.status_code == 201
    assert response.json()["username"] == "xiaofan"
    # 注册即登录：Cookie 已带上
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "xiaofan"
    assert me.json()["is_admin"] is False


def test_register_validates_credentials(auth_env, client):
    assert _register(client, username="好").status_code == 422
    assert _register(client, password="short").status_code == 422
    assert _register(client).status_code == 201
    assert _register(client).status_code == 409  # 用户名重复（大小写不敏感也走小写归一）
    assert _register(client, username="XiaoFan").status_code == 409


def test_login_logout_flow(auth_env, client):
    assert _register(client).status_code == 201
    client.post("/api/v1/auth/logout")

    me = client.get("/api/v1/auth/me")
    assert me.status_code == 401

    wrong = client.post("/api/v1/auth/login", json={"username": "xiaofan", "password": "wrong-pass"})
    assert wrong.status_code == 401

    ok = client.post("/api/v1/auth/login", json={"username": "xiaofan", "password": "password123"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200

    # 大小写不敏感登录
    other = _second_client()
    ok2 = other.post("/api/v1/auth/login", json={"username": "XiaoFan", "password": "password123"})
    assert ok2.status_code == 200


def test_disabled_user_cannot_login_or_use_session(auth_env, client):
    assert _register(client).status_code == 201
    with client.testing_session() as session:
        user = session.query(User).filter_by(username="xiaofan").one()
        user.is_active = False
        session.commit()

    response = client.post("/api/v1/auth/login", json={"username": "xiaofan", "password": "password123"})
    assert response.status_code == 401
    # 停用前签发的会话也立即失效
    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_session_rejected(auth_env, client):
    assert _register(client).status_code == 201
    # 直接把库里会话的过期时间改到过去
    with client.testing_session() as session:
        stored = session.query(UserSession).one()
        stored.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_data_isolated_between_users(auth_env, client):
    assert _register(client, username="alice").status_code == 201
    created = client.post("/api/v1/meals", json={"description": "板栗烧鸡"})
    assert created.status_code == 201
    meal_id = created.json()["id"]

    bob = _second_client()
    assert _register(bob, username="bob").status_code == 201
    assert bob.get("/api/v1/meals").json() == []
    # 试图动别人的记录：一律 404，不暴露存在性
    assert bob.delete(f"/api/v1/meals/{meal_id}").status_code == 404

    # 决策会话同样隔离
    alice_session = client.post("/api/v1/decisions?mode=takeout").json()["session_id"]
    assert bob.get(f"/api/v1/decisions/{alice_session}").status_code == 404
    assert bob.post(f"/api/v1/decisions/{alice_session}/answer?option=sweet").status_code == 404

    # alice 仍能看到自己的数据
    assert client.get("/api/v1/meals").json()[0]["description"] == "板栗烧鸡"


def test_non_admin_blocked_from_admin_api(auth_env, client):
    assert _register(client).status_code == 201
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.get("/api/v1/admin/overview").status_code == 403
    assert client.get("/api/v1/admin/users/1/summary").status_code == 403
    assert client.post("/api/v1/maintenance/prune", json={"days": 90}).status_code == 403


def test_admin_user_management(auth_env, client):
    # 管理员用 DB 种子方式创建（走 hash_password 正规路径）
    from app.accounts import hash_password

    with client.testing_session() as session:
        session.add(User(username="boss", password_hash=hash_password("admin-pass-123"), is_admin=True))
        session.commit()

    # 管理员登录
    assert client.post(
        "/api/v1/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    ).status_code == 200

    # 手动建号
    created = client.post(
        "/api/v1/admin/users",
        json={"username": "carol", "password": "carol-pass-123", "display_name": "卡罗尔"},
    )
    assert created.status_code == 201
    assert client.post(
        "/api/v1/admin/users", json={"username": "boss", "password": "whatever-123"}
    ).status_code == 409

    listing = client.get("/api/v1/admin/users")
    assert listing.status_code == 200
    usernames = {row["username"] for row in listing.json()}
    assert {"boss", "carol"} <= usernames

    # 重置密码：旧会话全部失效，新密码可登录
    carol_id = next(row["id"] for row in listing.json() if row["username"] == "carol")
    assert client.post(
        f"/api/v1/admin/users/{carol_id}/reset-password", json={"new_password": "new-pass-1234"}
    ).status_code == 200
    assert client.post(
        "/api/v1/admin/users", json={"username": "carol2", "password": "x"}
    ).status_code == 422

    # 停用 / 启用，且不能停用自己
    assert client.post(f"/api/v1/admin/users/{carol_id}/toggle-active").status_code == 200
    boss_id = next(row["id"] for row in listing.json() if row["username"] == "boss")
    assert client.post(f"/api/v1/admin/users/{boss_id}/toggle-active").status_code == 422

    overview = client.get("/api/v1/admin/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["users"]["total"] >= 2
    assert "ai_usage" in body

    summary = client.get(f"/api/v1/admin/users/{carol_id}/summary")
    assert summary.status_code == 200
    assert summary.json()["user"]["username"] == "carol"


def test_per_user_data_wipe(auth_env, client):
    assert _register(client, username="dave").status_code == 201
    client.post("/api/v1/meals", json={"description": "牛肉面"})
    client.post("/api/v1/preferences", json={"kind": "like", "topic": "牛肉", "text": "爱吃牛肉"})

    # 另一个用户的数据不受影响
    eve = _second_client()
    assert _register(eve, username="eve").status_code == 201
    eve.post("/api/v1/meals", json={"description": "沙拉"})

    response = client.request("DELETE", "/api/v1/data", json={"confirm": "DELETE"})
    assert response.status_code == 200
    assert client.get("/api/v1/meals").json() == []
    assert client.get("/api/v1/preferences").json() == []

    assert [m["description"] for m in eve.get("/api/v1/meals").json()] == ["沙拉"]


def test_export_only_own_data(auth_env, client):
    assert _register(client, username="frank").status_code == 201
    client.post("/api/v1/meals", json={"description": "饺子"})

    grace = _second_client()
    assert _register(grace, username="grace").status_code == 201
    grace.post("/api/v1/meals", json={"description": "火锅"})

    exported = client.get("/api/v1/export")
    assert exported.status_code == 200
    descriptions = [m["description"] for m in exported.json()["meals"]]
    assert descriptions == ["饺子"]
