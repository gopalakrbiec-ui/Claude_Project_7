"""add is_featured to templates

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-06-29 00:02:00.000000+00:00
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a1'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'templates',
        sa.Column('is_featured', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index('ix_templates_is_featured', 'templates', ['is_featured'])


def downgrade() -> None:
    op.drop_index('ix_templates_is_featured', table_name='templates')
    op.drop_column('templates', 'is_featured')
