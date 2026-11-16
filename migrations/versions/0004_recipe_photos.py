"""recipe photo catalog

Revision ID: 0004_recipe_photos
Revises: 0003_email_binding
Create Date: 2026-11-15

Store authorized object-storage image URLs and credits by normalized dish name.
"""

from __future__ import annotations

from alembic import op

revision = "0004_recipe_photos"
down_revision = "0003_email_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS recipe_photos (
            id SERIAL PRIMARY KEY,
            recipe_name VARCHAR(120) NOT NULL,
            image_url TEXT NOT NULL,
            image_alt VARCHAR(200) NOT NULL DEFAULT '',
            photographer VARCHAR(100) NOT NULL DEFAULT '',
            credit_name VARCHAR(100) NOT NULL DEFAULT '',
            credit_url TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_recipe_photos_recipe_name ON recipe_photos (recipe_name)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_recipe_photos_recipe_name")
    op.execute("DROP TABLE IF EXISTS recipe_photos")
