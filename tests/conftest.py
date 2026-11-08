from __future__ import annotations

import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.rate_limit import reset_rate_limits


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试用独立的 SQLite 文件，不碰真实 PostgreSQL。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        with TestingSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # Tests never depend on real .env credentials, tokens, budgets, or model access.
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(
            api_token="",
            is_production=False,
            admin_username="admin",
            admin_password="",
            invite_code="",
            session_ttl_days=30,
            dev_auto_login=True,
        ),
    )
    monkeypatch.setattr(
        "app.rate_limit.get_settings",
        lambda: SimpleNamespace(rate_limit_per_minute=10, auth_rate_limit_per_minute=5),
    )
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: SimpleNamespace(daily_token_budget=0, email_allowed_domains=""),
    )
    monkeypatch.setattr(
        "app.services.meal_assistant.get_settings",
        lambda: SimpleNamespace(
            openai_api_key="",
            openai_base_url="http://127.0.0.1:1/v1",
            openai_model="test-model",
            llm_price_per_1k_input=0.0,
            llm_price_per_1k_output=0.0,
        ),
    )
    # AI 城市菜层默认无 Key：测试里不发起真实模型调用
    monkeypatch.setattr(
        "app.services.city_dishes.get_settings",
        lambda: SimpleNamespace(
            openai_api_key="",
            openai_base_url="http://127.0.0.1:1/v1",
            openai_model="test-model",
            llm_price_per_1k_input=0.0,
            llm_price_per_1k_output=0.0,
        ),
    )
    # 不用 with：避免触发 lifespan 里对真实数据库的 create_all
    reset_rate_limits()
    test_client = TestClient(app)
    test_client.testing_session = TestingSession
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def email_mode(client, monkeypatch):
    """开启邮箱验证码注册/登录，假发信函数把 (收件人, 验证码, 用途) 收进 sent。

    依赖 client：让本 fixture 的补丁在 client 的补丁之后生效。
    state 允许用例运行中调整域名白名单。
    """
    sent = []
    state = {"allowed_domains": ""}

    def fake_send(to_email, code, purpose="register"):
        sent.append((to_email, code, purpose))

    def accounts_settings():
        return SimpleNamespace(
            admin_username="admin",
            admin_password="",
            admin_email="",
            session_ttl_days=30,
            email_code_ttl_minutes=10,
            email_code_resend_seconds=60,
            email_code_max_per_day=10,
        )

    def api_settings():
        return SimpleNamespace(
            daily_token_budget=0,
            email_allowed_domains=state["allowed_domains"],
        )

    def mailer_settings():
        return SimpleNamespace(
            smtp_host="smtp.qq.com",
            smtp_port=465,
            smtp_tls="ssl",
            smtp_user="noreply@qq.com",
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


@pytest.fixture()
def dev_user_id(client):
    """确保开发旁路用户存在并返回其 id，供测试直接插表时补归属列。"""
    from app.accounts import hash_password
    from app.models import User

    with client.testing_session() as session:
        user = session.query(User).filter_by(username="dev").one_or_none()
        if user is None:
            user = User(username="dev", password_hash=hash_password("dev12345"), is_admin=True)
            session.add(user)
            session.commit()
        return user.id


@pytest.fixture()
def fake_settings(monkeypatch):
    from types import SimpleNamespace
    from app.services import meal_assistant

    settings = SimpleNamespace(
        openai_api_key="test-key",
        openai_base_url="http://localhost:1/v1",
        openai_model="test-model",
        llm_price_per_1k_input=0.0,
        llm_price_per_1k_output=0.0,
    )
    monkeypatch.setattr(meal_assistant, "get_settings", lambda: settings)
    return settings
