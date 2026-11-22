"""One-use recovery codes for forgotten system-administrator passwords.

Revision ID: 0037_recovery_codes
Revises: 0036_password_must_change
Create Date: 2026-11-22

The owner generates a small set of one-use recovery codes from inside the
app; only their hashes are stored. A forgotten sysadmin password is then
recoverable offline with username + one unused code + a new password,
without email or any external service.
"""
from alembic import op
import sqlalchemy as sa


revision = "0037_recovery_codes"
down_revision = "0036_password_must_change"
branch_labels = None
depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("recovery_codes"):
        op.create_table(
            "recovery_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "admin_account_id",
                sa.Integer(),
                sa.ForeignKey("admin_accounts.id"),
                nullable=False,
            ),
            sa.Column("code_hash", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True)),
        )


def downgrade():
    op.drop_table("recovery_codes")
