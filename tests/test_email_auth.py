"""邮箱验证码注册测试：发码限频、验证码核销、邮箱登录、域名白名单。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

SMTP_USER = "noreply@qq.com"


@pytest.fixture()
def email_env(client, monkeypatch):
    """开启邮箱验证码注册；假发信函数把 (收件人, 验证码) 收进列表。

    依赖 client：让本 fixture 的补丁在 client 的补丁之后生效。
    """
    sent = []
    state = {"allowed_domains": ""}

    def fake_send(to_email, code):
        sent.append((to_email, code))

    def accounts_settings():
        return SimpleNamespace(
            admin_username="admin",
            admin_password="",
            admin_email="",
            invite_code="",
            session_ttl_days=30,
            email_code_ttl_minutes=10,
            email_code_resend_seconds=60,
            email_code_max_per_day=10,
        )

    def api_settings():
        return SimpleNamespace(
            daily_token_budget=0,
            invite_code="",
            email_allowed_domains=state["allowed_domains"],
        )

    def mailer_settings():
        return SimpleNamespace(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            smtp_tls="ssl",
            smtp_user=SMTP_USER,
            smtp_password="auth-code-123",
            email_from_name="饭搭子",
            email_code_ttl_minutes=10,
        )

    def security_settings():
        return SimpleNamespace(
            api_token="",
            is_production=False,
            admin_username="admin",
            admin_password="",
            invite_code="",
            session_ttl_days=30,
            dev_auto_login=False,
        )

    monkeypatch.setattr("app.accounts.get_settings", accounts_settings)
    monkeypatch.setattr("app.api.get_settings", api_settings)
    monkeypatch.setattr("app.mailer.get_settings", mailer_settings)
    monkeypatch.setattr("app.security.get_settings", security_settings)
    monkeypatch.setattr(
        "app.rate_limit.get_settings",
        lambda: SimpleNamespace(rate_limit_per_minute=10, auth_rate_limit_per_minute=0),
    )
    monkeypatch.setattr("app.api.send_verification_email", fake_send)
    return SimpleNamespace(sent=sent, state=state)


def _send_code(client, email):
    return client.post("/api/v1/auth/email-code", json={"email": email})


def _register_email(client, username, email, code, password="password123", **extra):
    body = {"username": username, "password": password, "email": email, "email_code": code, **extra}
    return client.post("/api/v1/auth/register", json=body)


def test_email_code_registration_flow(email_env, client):
    config = client.get("/api/v1/auth/config")
    assert config.json() == {"email_enabled": True, "invite_required": False}

    # 邮箱格式不对直接拒绝，不发信
    assert _send_code(client, "not-an-email").status_code == 422
    assert email_env.sent == []

    assert _send_code(client, "Alice@QQ.com").status_code == 200
    assert len(email_env.sent) == 1
    code = email_env.sent[0][1]
    assert email_env.sent[0][0] == "alice@qq.com"  # 归一化为小写

    # 没有验证码不能注册
    assert _register_email(client, "xiaofan", "alice@qq.com", "").status_code == 422
    # 错误验证码
    assert _register_email(client, "xiaofan", "alice@qq.com", "000000" if code != "000000" else "111111").status_code == 400
    # 正确验证码：注册即登录，邮箱绑定到账号
    assert _register_email(client, "xiaofan", "alice@qq.com", code).status_code == 201
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "alice@qq.com"

    # 验证码一次性 + 邮箱唯一：同一个邮箱不能再注册第二个号
    other = client.post(
        "/api/v1/auth/register",
        json={"username": "second", "password": "password123", "email": "alice@qq.com", "email_code": code},
    )
    assert other.status_code == 409


def test_resend_cooldown_and_daily_cap(email_env, client):
    assert _send_code(client, "bob@qq.com").status_code == 200
    # 60 秒冷却期内不能再发
    again = _send_code(client, "bob@qq.com")
    assert again.status_code == 429
    assert "等一会" in again.json()["detail"]

    # 把已有记录的时间倒退到冷却期之外，但 24 小时内已有 10 条 → 触发日上限
    from app.models import EmailVerification

    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    with client.testing_session() as session:
        rows = session.query(EmailVerification).filter_by(email="bob@qq.com").all()
        assert len(rows) == 1
        rows[0].created_at = past
        for i in range(9):
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


def test_expired_and_burned_codes(email_env, client):
    from app.models import EmailVerification

    assert _send_code(client, "carol@qq.com").status_code == 200
    code = email_env.sent[-1][1]

    # 过期
    with client.testing_session() as session:
        row = session.query(EmailVerification).filter_by(email="carol@qq.com").one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        row.created_at = datetime.now(timezone.utc) - timedelta(minutes=2)  # 顺便跳过发码冷却
        session.commit()
    expired = _register_email(client, "carol", "carol@qq.com", code)
    assert expired.status_code == 400
    assert "过期" in expired.json()["detail"]

    # 错 5 次后整条作废，即使后来输对也不行
    assert _send_code(client, "carol@qq.com").status_code == 200
    code = email_env.sent[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        response = _register_email(client, "carol", "carol@qq.com", wrong)
        assert response.status_code == 400
        assert "不对" in response.json()["detail"]
    burned = _register_email(client, "carol", "carol@qq.com", code)
    assert burned.status_code == 400
    assert "作废" in burned.json()["detail"]


def test_duplicate_email_rejected(email_env, client):
    assert _send_code(client, "dave@qq.com").status_code == 200
    assert _register_email(client, "dave", "dave@qq.com", email_env.sent[-1][1]).status_code == 201

    # 同邮箱再走注册：发码阶段就拦下
    duplicate = _send_code(client, "dave@qq.com")
    assert duplicate.status_code == 409
    assert "已经注册" in duplicate.json()["detail"]


def test_login_with_email(email_env, client):
    assert _send_code(client, "eve@qq.com").status_code == 200
    assert _register_email(client, "eve", "eve@qq.com", email_env.sent[-1][1]).status_code == 201
    client.post("/api/v1/auth/logout")

    ok = client.post("/api/v1/auth/login", json={"username": "eve@qq.com", "password": "password123"})
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me").json()["username"] == "eve"


def test_email_domain_allowlist(email_env, client):
    email_env.state["allowed_domains"] = "qq.com"
    assert _send_code(client, "frank@163.com").status_code == 422
    assert _send_code(client, "frank@qq.com").status_code == 200


def test_email_disabled_falls_back_to_invite(client, monkeypatch):
    # 不打 email_env 补丁：SMTP 未配置 → 邮箱注册关闭，回退邀请码
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: SimpleNamespace(daily_token_budget=0, invite_code="NEEDINVITE"),
    )
    config = client.get("/api/v1/auth/config")
    assert config.json() == {"email_enabled": False, "invite_required": True}
    assert _send_code(client, "grace@qq.com").status_code == 403

    response = client.post(
        "/api/v1/auth/register",
        json={"username": "grace", "password": "password123", "invite_code": "NEEDINVITE"},
    )
    assert response.status_code == 201
    assert response.json()["email"] is None


def test_admin_sees_email(email_env, client):
    from app.accounts import hash_password
    from app.models import User

    assert _send_code(client, "henry@qq.com").status_code == 200
    assert _register_email(client, "henry", "henry@qq.com", email_env.sent[-1][1]).status_code == 201

    with client.testing_session() as session:
        session.add(User(username="boss", password_hash=hash_password("admin-pass-123"), is_admin=True))
        session.commit()

    assert client.post(
        "/api/v1/auth/login", json={"username": "boss", "password": "admin-pass-123"}
    ).status_code == 200
    rows = client.get("/api/v1/admin/users").json()
    henry = next(row for row in rows if row["username"] == "henry")
    assert henry["email"] == "henry@qq.com"
