"""add template photo and category fields

Revision ID: a1b2c3d4e5f6
Revises: e735ed2b876f
Create Date: 2026-06-29 00:01:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'e735ed2b876f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('templates', sa.Column('category', sa.String(length=100), nullable=True))
    op.add_column('templates', sa.Column('image_url', sa.String(length=500), nullable=True))
    op.add_column('templates', sa.Column('scene_description', sa.String(length=1000), nullable=True))

    # Backfill category from theme for existing rows
    op.execute("UPDATE templates SET category = theme WHERE category IS NULL")

    # Make category non-nullable after backfill
    op.alter_column('templates', 'category', nullable=False)
    op.create_index('ix_templates_category', 'templates', ['category'])


def downgrade() -> None:
    op.drop_index('ix_templates_category', table_name='templates')
    op.drop_column('templates', 'scene_description')
    op.drop_column('templates', 'image_url')
    op.drop_column('templates', 'category')
