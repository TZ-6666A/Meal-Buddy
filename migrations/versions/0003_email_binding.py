"""email binding + email_verifications

Revision ID: 0003_email_binding
Revises: 0002_users_and_ownership
Create Date: 2026-11-08

引入邮箱绑定注册：
- users 增加 email 列（可空，老账号可以没有），加唯一索引；
- 新增 email_verifications 表存验证码哈希（发码限频、防爆破都在这张表上）。

DDL 全部幂等，可重复执行；只支持 PostgreSQL。
"""

from __future__ import annotations

from alembic import op

revision = "0003_email_binding"
down_revision = "0002_users_and_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email ON users (email)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verifications (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) NOT NULL,
            code_hash VARCHAR(64) NOT NULL,
            purpose VARCHAR(32) NOT NULL DEFAULT 'register',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_email_verifications_email ON email_verifications (email)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_email_verifications_email")
    op.execute("DROP TABLE IF EXISTS email_verifications")
    op.execute("DROP INDEX IF EXISTS uq_users_email")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email")
