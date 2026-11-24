"""Add unused evidence-preserving leave register metadata.

No data migration, accrual posting, account/permission changes or new routes.
An unknown imported blank stays NULL; explicit verified present is empty code.

Revision ID: 0042_leave_register_metadata
Revises: 0041_leave_foundation_facts
"""
from alembic import op
import sqlalchemy as sa

revision = "0042_leave_register_metadata"
down_revision = "0041_leave_foundation_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("leave_holidays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_leave_holiday_name"))
    op.create_index("ix_leave_holidays_holiday_date", "leave_holidays", ["holiday_date"])
    op.create_table("leave_register_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("code", sa.String(1), nullable=True),
        sa.Column("certificate_attached", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("leave_record_id", sa.Integer(), sa.ForeignKey("leave_records.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recorded_by", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("employee_id", "work_date", name="uq_leave_register_employee_day"),
        sa.CheckConstraint("code IS NULL OR code IN ('', 'U', 'A', 'S', 'H', 'W', '—')", name="ck_leave_register_code"),
        sa.CheckConstraint("length(trim(source)) > 0", name="ck_leave_register_source"),
        sa.CheckConstraint("revision >= 1", name="ck_leave_register_revision"))
    op.create_index("ix_leave_register_entries_employee_id", "leave_register_entries", ["employee_id"])
    op.create_index("ix_leave_register_entries_work_date", "leave_register_entries", ["work_date"])
    op.create_table("leave_month_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.String(80), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("employee_id", "year", "month", name="uq_leave_month_note_employee_period"),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name="ck_leave_month_note_month"),
        sa.CheckConstraint("year BETWEEN 1 AND 9999", name="ck_leave_month_note_year"))


def downgrade() -> None:
    op.drop_table("leave_month_notes")
    op.drop_table("leave_register_entries")
    op.drop_table("leave_holidays")
