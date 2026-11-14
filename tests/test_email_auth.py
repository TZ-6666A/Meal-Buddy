"""邮箱验证码注册/登录测试：发码限频、验证码核销、免密登录、域名白名单。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import EmailVerification, User


def _send_code(client, email, purpose="register"):
    return client.post("/api/v1/auth/email-code", json={"email": email, "purpose": purpose})


def _register_email(client, email, code, password="password123", **extra):
    body = {"email": email, "email_code": code, "password": password, **extra}
    return client.post("/api/v1/auth/register", json=body)


def _skip_cooldown(client, email):
    """把该邮箱已有验证码记录的时间倒退 2 分钟，绕开发码冷却（专测登录码时用）。"""
    with client.testing_session() as session:
        for row in session.query(EmailVerification).filter_by(email=email):
            row.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        session.commit()


def test_email_code_registration_flow(email_mode, client):
    config = client.get("/api/v1/auth/config")
    assert config.json() == {"email_enabled": True}

    # 邮箱格式不对直接拒绝，不发信
    assert _send_code(client, "not-an-email").status_code == 422
    assert email_mode.sent == []

    assert _send_code(client, "Alice@QQ.com").status_code == 200
    assert len(email_mode.sent) == 1
    code = email_mode.sent[0][1]
    assert email_mode.sent[0][0] == "alice@qq.com"  # 归一化为小写

    # 缺验证码 / 错误验证码
    assert _register_email(client, "alice@qq.com", "").status_code == 422
    wrong = "000000" if code != "000000" else "111111"
    assert _register_email(client, "alice@qq.com", wrong).status_code == 400

    # 正确验证码：注册即登录，邮箱即账号，昵称选填
    assert _register_email(
        client, "alice@qq.com", code, display_name="小饭"
    ).status_code == 201
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "alice@qq.com"
    assert me.json()["username"] == "alice@qq.com"
    assert me.json()["display_name"] == "小饭"

    # 一个邮箱只能绑定一个账号
    other = _register_email(client, "alice@qq.com", code)
    assert other.status_code == 409


def test_resend_cooldown_and_daily_cap(email_mode, client):
    assert _send_code(client, "bob@qq.com").status_code == 200
    # 60 秒冷却期内不能再发
    again = _send_code(client, "bob@qq.com")
    assert again.status_code == 429
    assert "等一会" in again.json()["detail"]

    # 把已有记录的时间倒退到冷却期之外，但 24 小时内已有 10 条 → 触发日上限
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    with client.testing_session() as session:
        rows = session.query(EmailVerification).filter_by(email="bob@qq.com").all()
        assert len(rows) == 1
        rows[0].created_at = past
        for _ in range(9):
            session.add(
                EmailVerification(
                    email="bob@qq.com", code_hash="stub", purpose="register",
                    created_at=past, expires_at=past + timedelta(minutes=10),
                )
            )
        session.commit()

    capped = _send_code(client, "bob@qq.com")
    assert capped.status_code == 429
    assert "明天" in capped.json()["detail"]


def test_expired_and_burned_codes(email_mode, client):
    assert _send_code(client, "carol@qq.com").status_code == 200
    code = email_mode.sent[-1][1]

    # 过期
    with client.testing_session() as session:
        row = session.query(EmailVerification).filter_by(email="carol@qq.com").one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)  # 顺便跳过发码冷却
        session.commit()
    expired = _register_email(client, "carol@qq.com", code)
    assert expired.status_code == 400
    assert "过期" in expired.json()["detail"]

    # 错 5 次后整条作废，即使后来输对也不行
    assert _send_code(client, "carol@qq.com").status_code == 200
    code = email_mode.sent[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        response = _register_email(client, "carol@qq.com", wrong)
        assert response.status_code == 400
        assert "不对" in response.json()["detail"]
    burned = _register_email(client, "carol@qq.com", code)
    assert burned.status_code == 400
    assert "作废" in burned.json()["detail"]


def test_duplicate_email_rejected(email_mode, client):
    assert _send_code(client, "dave@qq.com").status_code == 200
    assert _register_email(client, "dave@qq.com", email_mode.sent[-1][1]).status_code == 201

    # 同邮箱再走注册：发码阶段就拦下
    duplicate = _send_code(client, "dave@qq.com")
    assert duplicate.status_code == 409
    assert "已经注册" in duplicate.json()["detail"]


def test_login_with_email_password(email_mode, client):
    assert _send_code(client, "eve@qq.com").status_code == 200
    assert _register_email(client, "eve@qq.com", email_mode.sent[-1][1]).status_code == 201
    client.post("/api/v1/auth/logout")

    ok = client.post("/api/v1/auth/login", json={"username": "eve@qq.com", "password": "password123"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").json()["username"] == "eve@qq.com"


def test_email_code_login(email_mode, client):
    # 先注册一个用户并登出
    assert _send_code(client, "ken@qq.com").status_code == 200
    assert _register_email(client, "ken@qq.com", email_mode.sent[-1][1]).status_code == 201
    client.post("/api/v1/auth/logout")

    # 给未注册邮箱发登录码 → 404
    unregistered = client.post(
        "/api/v1/auth/email-code", json={"email": "ghost@qq.com", "purpose": "login"}
    )
    assert unregistered.status_code == 404

    # 注册码不能拿来登录（purpose 隔离）：先给未注册邮箱发注册码，
    # 随后该邮箱变成已注册（模拟管理员建号），旧注册码不得用于登录
    assert _send_code(client, "fresh@qq.com").status_code == 200
    with client.testing_session() as session:
        from app.accounts import hash_password

        session.add(User(username="fresh@qq.com", password_hash=hash_password("password123"), email="fresh@qq.com"))
        session.commit()
    wrong_purpose = client.post(
        "/api/v1/auth/login/email-code", json={"email": "fresh@qq.com", "email_code": email_mode.sent[-1][1]}
    )
    assert wrong_purpose.status_code == 400

    # 发登录码 → 验证码登录成功
    _skip_cooldown(client, "ken@qq.com")
    assert client.post(
        "/api/v1/auth/email-code", json={"email": "ken@qq.com", "purpose": "login"}
    ).status_code == 200
    code = email_mode.sent[-1][1]
    ok = client.post("/api/v1/auth/login/email-code", json={"email": "ken@qq.com", "email_code": code})
    assert ok.status_code == 200
    assert ok.json()["email"] == "ken@qq.com"
    cookie = ok.headers["set-cookie"].lower()
    assert "max-age=2592000" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert client.get("/api/v1/auth/me").status_code == 200
    with client.testing_session() as session:
        user = session.query(User).filter_by(username="ken@qq.com").one()
        assert user.last_login_at is not None

    # 验证码一次性
    client.post("/api/v1/auth/logout")
    replay = client.post("/api/v1/auth/login/email-code", json={"email": "ken@qq.com", "email_code": code})
    assert replay.status_code == 400


def test_email_code_reset_changes_password_and_invalidates_sessions(email_mode, client):
    assert _send_code(client, "reset@qq.com").status_code == 200
    assert _register_email(client, "reset@qq.com", email_mode.sent[-1][1]).status_code == 201
    other = client.__class__(client.app)
    assert other.post("/api/v1/auth/login", json={"username": "reset@qq.com", "password": "password123"}).status_code == 200

    client.post("/api/v1/auth/logout")
    _skip_cooldown(client, "reset@qq.com")
    assert _send_code(client, "reset@qq.com", purpose="reset").status_code == 200
    reset_code = email_mode.sent[-1][1]
    assert email_mode.sent[-1][2] == "reset"
    changed = client.post("/api/v1/auth/reset-password", json={
        "email": "reset@qq.com", "email_code": reset_code, "new_password": "new-password-123",
    })
    assert changed.status_code == 200
    assert client.post("/api/v1/auth/login", json={"username": "reset@qq.com", "password": "password123"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "reset@qq.com", "password": "new-password-123"}).status_code == 200
    assert other.get("/api/v1/auth/me").status_code == 401


def test_email_code_purposes_cannot_be_reused_for_reset(email_mode, client):
    assert _send_code(client, "purpose@qq.com").status_code == 200
    register_code = email_mode.sent[-1][1]
    assert _register_email(client, "purpose@qq.com", register_code).status_code == 201
    assert client.post("/api/v1/auth/reset-password", json={
        "email": "purpose@qq.com", "email_code": register_code, "new_password": "new-password-123",
    }).status_code == 400


def test_email_code_login_disabled_user(email_mode, client):
    assert _send_code(client, "ivy@qq.com").status_code == 200
    assert _register_email(client, "ivy@qq.com", email_mode.sent[-1][1]).status_code == 201
    with client.testing_session() as session:
        user = session.query(User).filter_by(username="ivy@qq.com").one()
        user.is_active = False
        session.commit()
    client.post("/api/v1/auth/logout")

    # 停用账号的邮箱仍能收到码，但登录被拒
    _skip_cooldown(client, "ivy@qq.com")
    assert client.post(
        "/api/v1/auth/email-code", json={"email": "ivy@qq.com", "purpose": "login"}
    ).status_code == 200
    response = client.post(
        "/api/v1/auth/login/email-code",
        json={"email": "ivy@qq.com", "email_code": email_mode.sent[-1][1]},
    )
    assert response.status_code == 401


def test_email_domain_allowlist(email_mode, client):
    email_mode.state["allowed_domains"] = "qq.com"
    assert _send_code(client, "frank@163.com").status_code == 422
    assert _send_code(client, "frank@qq.com").status_code == 200


def test_registration_closed_without_smtp(client, monkeypatch):
    # SMTP 未配置：注册整体关闭（邮箱验证码是唯一的注册方式）
    monkeypatch.setattr(
        "app.mailer.get_settings",
        lambda: SimpleNamespace(smtp_user="", smtp_password=""),
    )
    config = client.get("/api/v1/auth/config")
    assert config.json() == {"email_enabled": False}
    assert _send_code(client, "grace@qq.com").status_code == 403
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "grace@qq.com", "email_code": "123456", "password": "password123"},
    )
    assert response.status_code == 403
    assert "暂未开放" in response.json()["detail"]


def test_admin_sees_email(email_mode, client):
    from app.accounts import hash_password

    assert _send_code(client, "henry@qq.com").status_code == 200
    assert _register_email(client, "henry@qq.com", email_mode.sent[-1][1]).status_code == 201

    with client.testing_session() as session:
        session.add(User(username="boss", password_hash=hash_password("admin-pass-123"), is_admin=True))
        session.commit()

    assert client.post(
        "/api/v1/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    ).status_code == 200
    rows = client.get("/api/v1/admin/users").json()
    henry = next(row for row in rows if row["username"] == "henry@qq.com")
    assert henry["email"] == "henry@qq.com"
