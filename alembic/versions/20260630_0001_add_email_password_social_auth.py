"""add email, password, social auth columns to users

Revision ID: 20260630_0001
Revises: 20260629_0002_add_template_is_featured
Create Date: 2026-06-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260630_0001"
down_revision = "20260629_0002_add_template_is_featured"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make phone nullable — social/email users won't have a phone
    op.alter_column("users", "phone", nullable=True)

    # Make name nullable with default so existing rows are unaffected
    op.alter_column("users", "name", nullable=False, server_default="")

    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
    op.add_column("users", sa.Column("city", sa.String(200), nullable=True))
    op.add_column("users", sa.Column("hashed_password", sa.String(200), nullable=True))
    op.add_column("users", sa.Column("facebook_id", sa.String(100), nullable=True))

    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.create_unique_constraint("uq_users_facebook_id", "users", ["facebook_id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_facebook_id", "users", ["facebook_id"])


def downgrade() -> None:
    op.drop_index("ix_users_facebook_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_constraint("uq_users_facebook_id", "users")
    op.drop_constraint("uq_users_email", "users")
    op.drop_column("users", "facebook_id")
    op.drop_column("users", "hashed_password")
    op.drop_column("users", "city")
    op.drop_column("users", "email")
    op.alter_column("users", "phone", nullable=False)
