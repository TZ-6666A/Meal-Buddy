from __future__ import annotations

import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app


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
    monkeypatch.setattr("app.security.get_settings", lambda: SimpleNamespace(api_token=""))
    monkeypatch.setattr(
        "app.rate_limit.get_settings",
        lambda: SimpleNamespace(rate_limit_per_minute=10),
    )
    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: SimpleNamespace(daily_token_budget=0),
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
    # 不用 with：避免触发 lifespan 里对真实数据库的 create_all
    test_client = TestClient(app)
    test_client.testing_session = TestingSession
    yield test_client
    app.dependency_overrides.clear()


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
