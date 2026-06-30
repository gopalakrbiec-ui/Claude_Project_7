"""add email, password columns to users

Revision ID: 20260630_0001
Revises: b2c3d4e5f6a1
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260630_0001"
down_revision = "b2c3d4e5f6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make phone nullable — email/password users won't have a phone
    op.alter_column("users", "phone", nullable=True)

    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
    op.add_column("users", sa.Column("city", sa.String(200), nullable=True))
    op.add_column("users", sa.Column("hashed_password", sa.String(200), nullable=True))

    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_index("ix_users_email", "users", ["email"])


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.drop_constraint("uq_users_email", "users")
    op.drop_column("users", "hashed_password")
    op.drop_column("users", "city")
    op.drop_column("users", "email")
    op.alter_column("users", "phone", nullable=False)
