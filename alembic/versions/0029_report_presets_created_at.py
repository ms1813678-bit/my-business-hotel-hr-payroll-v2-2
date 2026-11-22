"""align report_presets.created_at with the model (NOT NULL)

Revision ID: 0029_report_presets_created_at
Revises: 0028_report_presets
"""
from alembic import op
import sqlalchemy as sa


revision = "0029_report_presets_created_at"
down_revision = "0028_report_presets"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "report_presets" not in sa.inspect(bind).get_table_names():
        return
    # The application always writes created_at (Python-side default); backfill
    # any historical NULL defensively before enforcing the constraint.
    bind.execute(
        sa.text("UPDATE report_presets SET created_at = NOW() WHERE created_at IS NULL")
    )
    op.alter_column("report_presets", "created_at", nullable=False)


def downgrade():
    # Keep the NOT NULL constraint: relaxing it would only re-admit rows the
    # application never writes.
    pass
