"""Keyed HMAC lookup column for recovery codes (timing-safe recovery).

Revision ID: 0039_recovery_code_lookup
Revises: 0038_admin_sessions
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0039_recovery_code_lookup"
down_revision = "0038_admin_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("recovery_codes", sa.Column("code_lookup", sa.String(length=64), nullable=True))
    op.create_index("ix_recovery_codes_code_lookup", "recovery_codes", ["code_lookup"])


def downgrade() -> None:
    op.drop_index("ix_recovery_codes_code_lookup", table_name="recovery_codes")
    op.drop_column("recovery_codes", "code_lookup")
