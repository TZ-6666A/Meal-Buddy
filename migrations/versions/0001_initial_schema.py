"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-25

覆盖饭搭子当前需要的全部表、索引、约束和 recommendation_feedback。

DDL 全部使用 IF NOT EXISTS：已有库（历史上由 create_all + sql/*.sql 建出来）
可以直接 `alembic upgrade head` 补齐，不需要先手工 stamp。
注意：表已存在时，CREATE TABLE IF NOT EXISTS 不会补约束；
已有的 recommendation_feedback 约束请对照 sql/20261024_lifecycle_up.sql 核对。

只支持 PostgreSQL。
"""

from __future__ import annotations

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS meal_entries (
            id SERIAL PRIMARY KEY,
            description TEXT NOT NULL,
            meal_type VARCHAR(40) NOT NULL DEFAULT 'unspecified',
            eaten_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pantry_items (
            id SERIAL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            quantity VARCHAR(80),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS preference_notes (
            id SERIAL PRIMARY KEY,
            kind VARCHAR(24) NOT NULL DEFAULT 'like',
            topic VARCHAR(60) NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_sessions (
            id SERIAL PRIMARY KEY,
            mode VARCHAR(24) NOT NULL DEFAULT 'takeout',
            condition VARCHAR(24) NOT NULL DEFAULT '',
            answers JSON NOT NULL DEFAULT '{}'::json,
            drawn JSON NOT NULL DEFAULT '[]'::json,
            step INTEGER NOT NULL DEFAULT 0,
            finished BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id SERIAL PRIMARY KEY,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_feedback (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES decision_sessions(id) ON DELETE CASCADE,
            dish_name VARCHAR(120) NOT NULL,
            action VARCHAR(16) NOT NULL,
            rating INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_feedback_session_dish UNIQUE (session_id, dish_name),
            CONSTRAINT ck_feedback_action CHECK (action IN ('accept', 'reject', 'eaten')),
            CONSTRAINT ck_feedback_rating CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5))
        )
        """
    )

    # 老库补列：这三张表在早期版本里没有 updated_at。
    op.execute("ALTER TABLE meal_entries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE pantry_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE preference_notes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")

    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_pantry_items_name ON pantry_items (name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_meal_entries_eaten_at ON meal_entries (eaten_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chat_messages_created_at ON chat_messages (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_decision_sessions_created_at ON decision_sessions (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_preference_notes_active ON preference_notes (active)")


def downgrade() -> None:
    # 会删除所有业务数据；先备份再执行，见 docs/migrations.md。
    op.execute("DROP TABLE IF EXISTS recommendation_feedback")
    op.execute("DROP TABLE IF EXISTS chat_messages")
    op.execute("DROP TABLE IF EXISTS decision_sessions")
    op.execute("DROP TABLE IF EXISTS preference_notes")
    op.execute("DROP TABLE IF EXISTS pantry_items")
    op.execute("DROP TABLE IF EXISTS meal_entries")
