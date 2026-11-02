"""PostgreSQL 集成测试。

只在设置了 TEST_DATABASE_URL 时运行；库名必须含 "test"，否则直接报错，
避免误连开发库或生产库。默认的 SQLite 单元测试不受影响。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app
from app.models import ChatMessage, DecisionSession, MealEntry, RecommendationFeedback

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
MIGRATION_REVISION = "0001_initial_schema"


def _assert_test_database(url: str) -> str:
    database = (make_url(url).database or "").lower()
    if "test" not in database:
        raise RuntimeError(
            f"TEST_DATABASE_URL 指向的库名 {database!r} 不含 'test'，拒绝运行集成测试。"
        )
    return database


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="未设置 TEST_DATABASE_URL，跳过 PostgreSQL 集成测试",
)


def _run_migrations(url: str, action: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        root = _project_root()
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        if action == "upgrade":
            command.upgrade(config, "head")
        elif action == "downgrade":
            command.downgrade(config, "base")
        else:  # pragma: no cover - 防御性
            raise ValueError(action)
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


def _project_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def pg_engine():
    database = _assert_test_database(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    # 从干净状态跑迁移，验证迁移本身能建出完整结构。
    if inspect(engine).has_table("alembic_version"):
        _run_migrations(TEST_DATABASE_URL, "downgrade")
    _run_migrations(TEST_DATABASE_URL, "upgrade")
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session_factory(pg_engine):
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with pg_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def pg_client(pg_session_factory, monkeypatch):
    def override_get_db():
        with pg_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: SimpleNamespace(api_token="", is_production=False),
    )
    monkeypatch.setattr(
        "app.rate_limit.get_settings", lambda: SimpleNamespace(rate_limit_per_minute=10)
    )
    monkeypatch.setattr("app.api.get_settings", lambda: SimpleNamespace(daily_token_budget=0))
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
    client = TestClient(app)
    client.testing_session = pg_session_factory
    yield client
    app.dependency_overrides.clear()


def test_migration_creates_full_schema(pg_engine):
    inspector = inspect(pg_engine)
    tables = set(inspector.get_table_names())
    assert {
        "meal_entries",
        "pantry_items",
        "preference_notes",
        "decision_sessions",
        "chat_messages",
        "recommendation_feedback",
    } <= tables

    meal_columns = {column["name"] for column in inspector.get_columns("meal_entries")}
    assert {"updated_at", "created_at", "eaten_at"} <= meal_columns

    index_names = {index["name"] for index in inspector.get_indexes("meal_entries")}
    assert "ix_meal_entries_eaten_at" in index_names

    checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("recommendation_feedback")
    }
    assert {"ck_feedback_action", "ck_feedback_rating"} <= checks

    with pg_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == MIGRATION_REVISION


def test_meal_crud_roundtrip(pg_client):
    created = pg_client.post(
        "/api/v1/meals",
        json={"description": "番茄炒蛋", "meal_type": "晚餐"},
    )
    assert created.status_code == 201
    meal_id = created.json()["id"]

    listed = pg_client.get("/api/v1/meals")
    assert [item["description"] for item in listed.json()] == ["番茄炒蛋"]

    deleted = pg_client.delete(f"/api/v1/meals/{meal_id}")
    assert deleted.status_code == 204
    assert pg_client.get("/api/v1/meals").json() == []


def test_preference_conflict_deactivates_old_note(pg_client):
    pg_client.post("/api/v1/preferences", json={"kind": "like", "topic": "牛肉", "text": "爱吃牛肉"})
    pg_client.post("/api/v1/preferences", json={"kind": "avoid", "topic": "牛肉", "text": "不吃牛肉"})

    active = pg_client.get("/api/v1/preferences").json()
    assert [(item["kind"], item["topic"]) for item in active] == [("avoid", "牛肉")]

    with pg_client.testing_session() as session:
        rows = session.execute(
            text("SELECT kind, active FROM preference_notes ORDER BY id")
        ).all()
    assert rows == [("like", False), ("avoid", True)]


def test_feedback_rejects_unknown_dish_and_updates_duplicate(pg_client):
    with pg_client.testing_session() as session:
        decision = DecisionSession(mode="takeout", drawn=["番茄炒蛋"], finished=True)
        session.add(decision)
        session.commit()
        session_id = decision.id

    url = f"/api/v1/decisions/{session_id}/feedback"
    assert pg_client.post(url, json={"dish_name": "别的菜", "action": "accept"}).status_code == 422
    assert pg_client.post(url, json={"dish_name": "番茄炒蛋", "action": "accept"}).status_code == 200
    second = pg_client.post(url, json={"dish_name": "番茄炒蛋", "action": "reject", "rating": 2})
    assert second.status_code == 200
    assert second.json()["action"] == "reject"

    with pg_client.testing_session() as session:
        assert session.query(RecommendationFeedback).count() == 1


def test_feedback_check_constraints_are_enforced_by_database(pg_client):
    with pg_client.testing_session() as session:
        decision = DecisionSession(mode="takeout", drawn=["番茄炒蛋"], finished=True)
        session.add(decision)
        session.commit()
        session_id = decision.id

    with pytest.raises(IntegrityError):
        with pg_client.testing_session() as session:
            session.add(
                RecommendationFeedback(
                    session_id=session_id, dish_name="番茄炒蛋", action="invalid", rating=None
                )
            )
            session.commit()

    with pytest.raises(IntegrityError):
        with pg_client.testing_session() as session:
            session.add(
                RecommendationFeedback(
                    session_id=session_id, dish_name="番茄炒蛋", action="accept", rating=9
                )
            )
            session.commit()

    with pytest.raises(IntegrityError):
        with pg_client.testing_session() as session:
            session.add(
                RecommendationFeedback(session_id=999999, dish_name="无会话", action="accept")
            )
            session.commit()


def test_export_and_delete_all(pg_client):
    pg_client.post("/api/v1/meals", json={"description": "番茄炒蛋"})
    pg_client.post("/api/v1/pantry", json={"name": "番茄"})
    pg_client.post("/api/v1/preferences", json={"kind": "like", "topic": "土豆", "text": "爱吃土豆"})

    exported = pg_client.get("/api/v1/export").json()
    assert exported["meals"] and exported["pantry"] and exported["preferences"]
    assert "exported_at" in exported

    assert pg_client.request("DELETE", "/api/v1/data", json={"confirm": "no"}).status_code == 400
    cleared = pg_client.request("DELETE", "/api/v1/data", json={"confirm": "DELETE"})
    assert cleared.status_code == 200
    assert cleared.json()["deleted"]["meal_entries"] == 1

    after = pg_client.get("/api/v1/export").json()
    assert after["meals"] == [] and after["pantry"] == [] and after["preferences"] == []


def test_prune_removes_only_old_rows(pg_client):
    old = datetime.now(timezone.utc) - timedelta(days=120)
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    with pg_client.testing_session() as session:
        session.add_all(
            [
                ChatMessage(role="user", content="旧消息", created_at=old),
                ChatMessage(role="agent", content="新消息", created_at=recent),
                DecisionSession(mode="takeout", finished=True, created_at=old),
                DecisionSession(mode="takeout", finished=False, created_at=old),
            ]
        )
        session.commit()

    result = pg_client.post("/api/v1/maintenance/prune", json={"days": 90})
    assert result.status_code == 200
    assert result.json()["chat_messages"] == 1
    assert result.json()["decision_sessions"] == 1

    with pg_client.testing_session() as session:
        assert session.query(ChatMessage).count() == 1
        remaining = session.query(DecisionSession).all()
        assert len(remaining) == 1 and remaining[0].finished is False
