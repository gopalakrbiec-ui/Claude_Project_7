"""add signup_bonus value to ledger_reason enum

Revision ID: 20260706_0001
Revises: 20260630_0001
Create Date: 2026-07-06
"""
from __future__ import annotations

from alembic import op

revision = "20260706_0001"
down_revision = "20260630_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block in
    # PostgreSQL — autocommit_block() runs this statement outside the
    # migration's normal transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE ledger_reason ADD VALUE IF NOT EXISTS 'signup_bonus'")


def downgrade() -> None:
    # PostgreSQL does not support removing a value from an enum type.
    # A downgrade would require recreating the type and remapping all rows —
    # not done here since signup_bonus rows may already exist in production.
    pass
