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
        lambda: SimpleNamespace(daily_token_budget=0, invite_code=""),
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
