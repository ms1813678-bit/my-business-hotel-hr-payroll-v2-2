"""Add unused leave close metadata referencing the host backup record.

No automatic closing, role selection, schedule, policy default or data changes.
A future authorized workflow must verify backup success and audit atomically.

Revision ID: 0043_leave_close_metadata
Revises: 0042_leave_register_metadata
"""
from alembic import op
import sqlalchemy as sa

revision = "0043_leave_close_metadata"
down_revision = "0042_leave_register_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("leave_month_close",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("backup_run_id", sa.Integer(), sa.ForeignKey("backup_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("closed_by", sa.String(80), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_snapshot_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("year", "month", name="uq_leave_close_period"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="ck_leave_close_month"),
        sa.CheckConstraint("year BETWEEN 1 AND 9999", name="ck_leave_close_year"),
        sa.CheckConstraint("length(trim(closed_by)) > 0", name="ck_leave_close_actor"))


def downgrade() -> None:
    op.drop_table("leave_month_close")
