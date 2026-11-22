"""Flag archive-resurrected accounts as locked pending a password reset.

A restore can bring back accounts that were deleted after the backup (or that
a fresh install's bootstrap snapshot never had). They return inactive with
must_change_password set, but the archive-time password hash is still on the
row: a bare re-activation would let a stolen old password sign in once.
This flag makes the accounts page refuse plain re-activation for them; the
account only comes back through the audited reset flow, which destroys the
old hash, activates the account and clears the flag (owner-approved
2026-11-23).

Revision ID: 0040_pending_credential_reset
Revises: 0039_recovery_code_lookup
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_pending_credential_reset"
down_revision = "0039_recovery_code_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_accounts",
        sa.Column(
            "pending_credential_reset",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("admin_accounts", "pending_credential_reset")
