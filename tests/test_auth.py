"""登录系统测试：邮箱注册（邮箱即账号）、登录、会话、数据隔离、管理员接口。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import User, UserSession


def _register(email_mode, client, email="xiaofan@qq.com", password="password123", display_name=None):
    """走完整邮箱注册流：发码 → 取码 → 注册。"""
    response = client.post("/api/v1/auth/email-code", json={"email": email})
    assert response.status_code == 200, response.text
    body = {"email": email, "email_code": email_mode.sent[-1][1], "password": password}
    if display_name:
        body["display_name"] = display_name
    return client.post("/api/v1/auth/register", json=body)


def _second_client() -> TestClient:
    """共享依赖覆盖、独立 Cookie 罐的第二个客户端，用来模拟另一个用户。"""
    return TestClient(app)


def test_register_requires_email_code(email_mode, client):
    # 缺验证码 / 错误验证码都不能注册
    missing = client.post(
        "/api/v1/auth/register", json={"email": "a1@qq.com", "email_code": "", "password": "password123"}
    )
    assert missing.status_code == 422
    wrong = client.post(
        "/api/v1/auth/register", json={"email": "a1@qq.com", "email_code": "000000", "password": "password123"}
    )
    assert wrong.status_code == 400

    # 正常注册：注册即登录，邮箱即账号
    assert _register(email_mode, client).status_code == 201
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "xiaofan@qq.com"
    assert me.json()["username"] == "xiaofan@qq.com"
    assert me.json()["is_admin"] is False


def test_register_validates(email_mode, client):
    assert client.post("/api/v1/auth/email-code", json={"email": "xiaofan@qq.com"}).status_code == 200
    code = email_mode.sent[-1][1]
    # 密码太短被拒；改正后用同一个验证码注册成功
    short = client.post(
        "/api/v1/auth/register", json={"email": "xiaofan@qq.com", "email_code": code, "password": "short"}
    )
    assert short.status_code == 422
    ok = client.post(
        "/api/v1/auth/register", json={"email": "xiaofan@qq.com", "email_code": code, "password": "password123"}
    )
    assert ok.status_code == 201
    # 一个邮箱只能绑定一个账号（大小写归一后判定），重复注册无论码对不对都被拦
    dup = client.post(
        "/api/v1/auth/register",
        json={"email": "XiaoFan@QQ.com", "email_code": "000000", "password": "password123"},
    )
    assert dup.status_code == 409
    # 昵称选填，换邮箱可以继续注册
    assert _register(email_mode, client, email="nick@qq.com", display_name="小饭").status_code == 201


def test_login_logout_flow(email_mode, client):
    assert _register(email_mode, client).status_code == 201
    client.post("/api/v1/auth/logout")

    assert client.get("/api/v1/auth/me").status_code == 401

    wrong = client.post("/api/v1/auth/login", json={"username": "xiaofan@qq.com", "password": "wrong-pass"})
    assert wrong.status_code == 401

    ok = client.post("/api/v1/auth/login", json={"username": "xiaofan@qq.com", "password": "password123"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200

    # 邮箱统一小写存储，大小写写法都能登录
    other = _second_client()
    ok2 = other.post("/api/v1/auth/login", json={"username": "XiaoFan@QQ.com", "password": "password123"})
    assert ok2.status_code == 200


def test_disabled_user_cannot_login_or_use_session(email_mode, client):
    assert _register(email_mode, client).status_code == 201
    with client.testing_session() as session:
        user = session.query(User).filter_by(username="xiaofan@qq.com").one()
        user.is_active = False
        session.commit()

    response = client.post(
        "/api/v1/auth/login", json={"username": "xiaofan@qq.com", "password": "password123"}
    )
    assert response.status_code == 401
    # 停用前签发的会话也立即失效
    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_session_rejected(email_mode, client):
    assert _register(email_mode, client).status_code == 201
    # 直接把库里会话的过期时间改到过去
    with client.testing_session() as session:
        stored = session.query(UserSession).one()
        stored.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()
    assert client.get("/api/v1/auth/me").status_code == 401


def test_data_isolated_between_users(email_mode, client):
    assert _register(email_mode, client, email="alice@qq.com").status_code == 201
    created = client.post("/api/v1/meals", json={"description": "板栗烧鸡"})
    assert created.status_code == 201
    meal_id = created.json()["id"]

    bob = _second_client()
    assert _register(email_mode, bob, email="bob@qq.com").status_code == 201
    assert bob.get("/api/v1/meals").json() == []
    # 试图动别人的记录：一律 404，不暴露存在性
    assert bob.delete(f"/api/v1/meals/{meal_id}").status_code == 404

    # 决策会话同样隔离
    alice_session = client.post("/api/v1/decisions?mode=takeout").json()["session_id"]
    assert bob.get(f"/api/v1/decisions/{alice_session}").status_code == 404
    assert bob.post(f"/api/v1/decisions/{alice_session}/answer?option=sweet").status_code == 404

    # alice 仍能看到自己的数据
    assert client.get("/api/v1/meals").json()[0]["description"] == "板栗烧鸡"


def test_non_admin_blocked_from_admin_api(email_mode, client):
    assert _register(email_mode, client).status_code == 201
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.get("/api/v1/admin/overview").status_code == 403
    assert client.get("/api/v1/admin/users/1/summary").status_code == 403
    assert client.post("/api/v1/maintenance/prune", json={"days": 90}).status_code == 403


def test_admin_user_management(email_mode, client):
    # 管理员用 DB 种子方式创建（走 hash_password 正规路径），仍以用户名登录
    from app.accounts import hash_password

    with client.testing_session() as session:
        session.add(User(username="boss", password_hash=hash_password("admin-pass-123"), is_admin=True))
        session.commit()

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


def test_per_user_data_wipe(email_mode, client):
    assert _register(email_mode, client, email="dave@qq.com").status_code == 201
    client.post("/api/v1/meals", json={"description": "牛肉面"})
    client.post("/api/v1/preferences", json={"kind": "like", "topic": "牛肉", "text": "爱吃牛肉"})

    # 另一个用户的数据不受影响
    eve = _second_client()
    assert _register(email_mode, eve, email="eve@qq.com").status_code == 201
    eve.post("/api/v1/meals", json={"description": "沙拉"})

    response = client.request("DELETE", "/api/v1/data", json={"confirm": "DELETE"})
    assert response.status_code == 200
    assert client.get("/api/v1/meals").json() == []
    assert client.get("/api/v1/preferences").json() == []

    assert [m["description"] for m in eve.get("/api/v1/meals").json()] == ["沙拉"]


def test_export_only_own_data(email_mode, client):
    assert _register(email_mode, client, email="frank@qq.com").status_code == 201
    client.post("/api/v1/meals", json={"description": "饺子"})

    grace = _second_client()
    assert _register(email_mode, grace, email="grace@qq.com").status_code == 201
    grace.post("/api/v1/meals", json={"description": "火锅"})

    exported = client.get("/api/v1/export")
    assert exported.status_code == 200
    descriptions = [m["description"] for m in exported.json()["meals"]]
    assert descriptions == ["饺子"]
