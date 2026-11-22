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
    # Rows created before this migration carry no HMAC lookup, so their owner
    # falls back to a per-row verification loop whose cost differs from the
    # unknown-user path. That leak only dies when every legacy row is replaced,
    # and generation already invalidates all previous codes — so arm the same
    # reconciliation gate used after a backup restore and confine the system
    # admin until codes are regenerated once. Fresh installs (no rows) skip this.
    op.execute(
        """
        INSERT INTO system_settings (key, value, updated_by, updated_at)
        SELECT 'post_restore_regen_required', 'legacy_codes', 'migration-0039', now()
        WHERE EXISTS (SELECT 1 FROM recovery_codes)
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_recovery_codes_code_lookup", table_name="recovery_codes")
    op.drop_column("recovery_codes", "code_lookup")
