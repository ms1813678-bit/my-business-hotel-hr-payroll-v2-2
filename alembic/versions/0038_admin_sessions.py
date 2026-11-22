"""Server-side session records for session inventory and manual termination.

Revision ID: 0038_admin_sessions
Revises: 0037_recovery_codes
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_admin_sessions"
down_revision = "0037_recovery_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("admin_account_id", sa.Integer(), sa.ForeignKey("admin_accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
    )
    op.create_index("ix_admin_sessions_admin_account_id", "admin_sessions", ["admin_account_id"])


def downgrade() -> None:
    op.drop_index("ix_admin_sessions_admin_account_id", table_name="admin_sessions")
    op.drop_table("admin_sessions")
