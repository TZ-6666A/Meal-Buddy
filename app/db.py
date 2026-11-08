from collections.abc import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


REQUIRED_TABLES = (
    "users",
    "user_sessions",
    "email_verifications",
    "meal_entries",
    "pantry_items",
    "preference_notes",
    "decision_sessions",
    "chat_messages",
    "recommendation_feedback",
)


def missing_tables() -> list[str]:
    """返回库里缺失的表；用于启动时提示先跑迁移。"""
    existing = set(inspect(engine).get_table_names())
    return [name for name in REQUIRED_TABLES if name not in existing]

