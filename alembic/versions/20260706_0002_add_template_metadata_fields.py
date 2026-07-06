"""add negative_prompt, tags, aspect_ratio to templates

Revision ID: 20260706_0002
Revises: 20260706_0001
Create Date: 2026-07-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260706_0002"
down_revision = "20260706_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("templates", sa.Column("negative_prompt", sa.Text(), nullable=True))
    op.add_column(
        "templates",
        sa.Column("tags", JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "templates",
        sa.Column("aspect_ratio", sa.String(10), nullable=False, server_default="9:16"),
    )


def downgrade() -> None:
    op.drop_column("templates", "aspect_ratio")
    op.drop_column("templates", "tags")
    op.drop_column("templates", "negative_prompt")
