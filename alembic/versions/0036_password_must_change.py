"""Forced password change after an administrator-initiated reset.

Revision ID: 0036_password_must_change
Revises: 0035_account_roles
Create Date: 2026-11-22

When the system administrator resets an account's password, the account gets
a generated one-time password and this flag; the next login must replace it
with a personal password before any other page opens. Self-service password
changes clear the flag.
"""
from alembic import op
import sqlalchemy as sa


revision = "0036_password_must_change"
down_revision = "0035_account_roles"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("admin_accounts")
    }
    if "must_change_password" not in columns:
        op.add_column(
            "admin_accounts",
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            ),
        )


def downgrade():
    op.drop_column("admin_accounts", "must_change_password")
