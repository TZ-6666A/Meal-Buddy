"""users + per-user ownership

Revision ID: 0002_users_and_ownership
Revises: 0001_initial_schema
Create Date: 2026-11-08

引入登录系统：
- 新增 users / user_sessions 两张表；
- 五张业务表增加 user_id 归属列（历史行先允许 NULL，启动引导会回填给管理员）；
- pantry_items 的全局唯一 name 索引改为 (user_id, lower(name)) 按用户唯一；
- 幂等补外键（ON DELETE CASCADE）。

DDL 全部幂等，可重复执行；只支持 PostgreSQL。
user_id 列保持可空是为了让迁移先于启动引导跑完——
app.accounts.ensure_bootstrap 会把 NULL 全部归到管理员名下。

回滚会删除 users/user_sessions 并丢失数据归属信息，先备份再执行。
"""

from __future__ import annotations

from alembic import op

revision = "0002_users_and_ownership"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

_OWNED_TABLES = (
    "meal_entries",
    "pantry_items",
    "preference_notes",
    "decision_sessions",
    "chat_messages",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(40) NOT NULL,
            password_hash VARCHAR(200) NOT NULL,
            display_name VARCHAR(40),
            is_admin BOOLEAN NOT NULL DEFAULT false,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id SERIAL PRIMARY KEY,
            token_hash VARCHAR(64) NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    for table in _OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS user_id BIGINT")

    # 老的全局唯一食材名索引退役，换成按用户唯一
    op.execute("DROP INDEX IF EXISTS ix_pantry_items_name")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pantry_user_name ON pantry_items (user_id, lower(name))"
    )

    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sessions_token_hash ON user_sessions (token_hash)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_sessions_user_id ON user_sessions (user_id)")
    for table in _OWNED_TABLES:
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_user_id ON {table} (user_id)")

    # 幂等补外键：user_id 此时可能有历史 NULL 行（引导回填前），外键允许 NULL
    op.execute(
        """
        DO $$
        DECLARE
            pair RECORD;
        BEGIN
            FOR pair IN
                SELECT * FROM (VALUES
                    ('meal_entries', 'fk_meal_entries_user'),
                    ('pantry_items', 'fk_pantry_items_user'),
                    ('preference_notes', 'fk_preference_notes_user'),
                    ('decision_sessions', 'fk_decision_sessions_user'),
                    ('chat_messages', 'fk_chat_messages_user'),
                    ('user_sessions', 'fk_user_sessions_user')
                ) AS t(tbl, cname)
            LOOP
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = pair.cname) THEN
                    EXECUTE format(
                        'ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (user_id) '
                        'REFERENCES users(id) ON DELETE CASCADE',
                        pair.tbl, pair.cname
                    );
                END IF;
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    # 删除 users 会连带失去数据归属信息；先备份再执行，见 docs/migrations.md。
    for table in _OWNED_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_user")
    op.execute("ALTER TABLE user_sessions DROP CONSTRAINT IF EXISTS fk_user_sessions_user")
    for table in _OWNED_TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_user_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS user_id")
    op.execute("DROP INDEX IF EXISTS uq_pantry_user_name")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_pantry_items_name ON pantry_items (name)")
    op.execute("DROP TABLE IF EXISTS user_sessions")
    op.execute("DROP TABLE IF EXISTS users")
