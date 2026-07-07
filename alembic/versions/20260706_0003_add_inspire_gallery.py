"""add inspire_gallery table

Revision ID: 20260706_0003
Revises: 20260706_0002
Create Date: 2026-07-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260706_0003"
down_revision = "20260706_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspire_gallery",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("image_url", sa.String(500), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_inspire_gallery_category", "inspire_gallery", ["category"])
    op.create_index("ix_inspire_gallery_active", "inspire_gallery", ["active"])


def downgrade() -> None:
    op.drop_index("ix_inspire_gallery_active", table_name="inspire_gallery")
    op.drop_index("ix_inspire_gallery_category", table_name="inspire_gallery")
    op.drop_table("inspire_gallery")
