"""optional Chinese display name for leave types

Revision ID: 0030_leave_type_name_zh
Revises: 0029_report_presets_created_at
"""
from alembic import op
import sqlalchemy as sa


revision = "0030_leave_type_name_zh"
down_revision = "0029_report_presets_created_at"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("leave_types")}
    if "name_zh" not in columns:
        op.add_column("leave_types", sa.Column("name_zh", sa.String(100), nullable=True))


def downgrade():
    # Retain the column: dropping it would discard stored Chinese names.
    pass
