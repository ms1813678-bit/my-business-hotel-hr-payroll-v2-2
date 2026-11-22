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
    # The application always writes created_at (Python-side default), so a
    # NULL here means hand-edited data. Never invent a historical timestamp:
    # fail loudly and let the operator set the real value, then re-run.
    null_count = bind.execute(
        sa.text("SELECT count(*) FROM report_presets WHERE created_at IS NULL")
    ).scalar()
    if null_count:
        raise RuntimeError(
            f"report_presets has {null_count} row(s) with NULL created_at; "
            "set their real values manually, then re-run the migration."
        )
    op.alter_column("report_presets", "created_at", nullable=False)


def downgrade():
    # Keep the NOT NULL constraint: relaxing it would only re-admit rows the
    # application never writes.
    pass
