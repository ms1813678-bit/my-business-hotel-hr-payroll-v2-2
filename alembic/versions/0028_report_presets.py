"""add saved report presets

Revision ID: 0028_report_presets
Revises: 0027_employee_nationality
"""
from alembic import op
import sqlalchemy as sa


revision = "0028_report_presets"
down_revision = "0027_employee_nationality"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "report_presets" not in inspector.get_table_names():
        op.create_table(
            "report_presets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("config", sa.Text(), nullable=False),
            sa.Column("created_by", sa.String(80), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("name", name="uq_report_presets_name"),
        )


def downgrade():
    # Retain the table: dropping it would discard saved report presets.
    pass
