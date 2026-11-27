"""Named accounts with roles: system admin, finance, assistant manager.

Revision ID: 0035_account_roles
Revises: 0034_payment_correction
Create Date: 2026-11-22

The historical single-admin constraints (one row only, always active) are
replaced by named personal accounts with a role column. The existing account
keeps its credentials and becomes the system administrator; sessions keep
working because the table, username and session_version semantics are
unchanged. Accounts are deactivated, never deleted.
"""
from alembic import op
import sqlalchemy as sa


revision = "0035_account_roles"
down_revision = "0034_payment_correction"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("admin_accounts")
    }
    if "role" not in columns:
        op.add_column(
            "admin_accounts",
            sa.Column("role", sa.String(20), server_default="system_admin", nullable=False),
        )
    if "display_name" not in columns:
        op.add_column("admin_accounts", sa.Column("display_name", sa.String(120)))
    inspector = sa.inspect(op.get_bind())
    unique_names = {c["name"] for c in inspector.get_unique_constraints("admin_accounts")}
    check_names = {c["name"] for c in inspector.get_check_constraints("admin_accounts")}
    if "uq_admin_accounts_singleton_key" in unique_names:
        op.drop_constraint("uq_admin_accounts_singleton_key", "admin_accounts", type_="unique")
    if "ck_admin_accounts_singleton_key" in check_names:
        op.drop_constraint("ck_admin_accounts_singleton_key", "admin_accounts", type_="check")
    if "ck_admin_accounts_must_remain_active" in check_names:
        op.drop_constraint("ck_admin_accounts_must_remain_active", "admin_accounts", type_="check")
    op.create_check_constraint(
        "ck_admin_accounts_role",
        "admin_accounts",
        "role IN ('system_admin', 'finance', 'assistant_manager')",
    )


def downgrade():
    # Named accounts are never deleted (see the module docstring). Going back to the
    # single-account schema is only possible while at most one account exists.
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT count(*) FROM admin_accounts")) > 1:
        raise RuntimeError("Refusing downgrade: named accounts would be deleted.")
    first_id = bind.scalar(sa.text("SELECT min(id) FROM admin_accounts"))
    if bind.dialect.name == "postgresql":
        # The no-delete trigger protects accounts at runtime; restoring the
        # single-account schema requires removing the extra rows it blocks.
        op.execute("DROP TRIGGER IF EXISTS trg_prevent_admin_account_deletion ON admin_accounts")
    bind.execute(
        sa.text("DELETE FROM admin_accounts WHERE id <> :first_id"),
        {"first_id": first_id},
    )
    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE TRIGGER trg_prevent_admin_account_deletion
            BEFORE DELETE ON admin_accounts FOR EACH ROW
            EXECUTE FUNCTION prevent_admin_account_deletion()
        """)
    bind.execute(
        sa.text("UPDATE admin_accounts SET active = true, singleton_key = 1 WHERE id = :first_id"),
        {"first_id": first_id},
    )
    op.drop_constraint("ck_admin_accounts_role", "admin_accounts", type_="check")
    op.create_check_constraint(
        "ck_admin_accounts_must_remain_active", "admin_accounts", "active IS TRUE"
    )
    op.create_check_constraint(
        "ck_admin_accounts_singleton_key", "admin_accounts", "singleton_key = 1"
    )
    op.create_unique_constraint(
        "uq_admin_accounts_singleton_key", "admin_accounts", ["singleton_key"]
    )
    op.drop_column("admin_accounts", "display_name")
    op.drop_column("admin_accounts", "role")
