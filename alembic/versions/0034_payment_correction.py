"""Audited correction links for payroll payment records.

Revision ID: 0034_payment_correction
Revises: 0033_absence_leave_review
Create Date: 2026-11-22

A mistaken payment record is never edited or deleted: the original entry is
marked superseded by its correcting entry, which links back to it and carries
the recorded reason. Existing payments stay active (both columns NULL).
"""
from alembic import op
import sqlalchemy as sa


revision = "0034_payment_correction"
down_revision = "0033_absence_leave_review"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("payroll_payments")
    }
    if "superseded_by_payment_id" not in columns:
        op.add_column(
            "payroll_payments",
            sa.Column(
                "superseded_by_payment_id",
                sa.Integer(),
                sa.ForeignKey("payroll_payments.id", ondelete="RESTRICT"),
            ),
        )
    if "correction_of_id" not in columns:
        op.add_column(
            "payroll_payments",
            sa.Column(
                "correction_of_id",
                sa.Integer(),
                sa.ForeignKey("payroll_payments.id", ondelete="RESTRICT"),
            ),
        )
    if "correction_reason" not in columns:
        op.add_column(
            "payroll_payments",
            sa.Column("correction_reason", sa.String(300)),
        )


def downgrade():
    op.drop_column("payroll_payments", "correction_reason")
    op.drop_column("payroll_payments", "correction_of_id")
    op.drop_column("payroll_payments", "superseded_by_payment_id")
