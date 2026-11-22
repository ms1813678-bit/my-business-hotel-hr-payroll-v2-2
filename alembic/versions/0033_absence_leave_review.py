"""Track HR review of absence/paid-leave conflicts on payroll items.

Revision ID: 0033_absence_leave_review
Revises: 0032_ai_language_provenance
Create Date: 2026-11-22

Existing items are stamped unreviewed (false): a historical absence deduction
alongside confirmed paid leave must pass a human review before a draft run can
be approved, which is exactly the protective behavior being added.
"""
from alembic import op
import sqlalchemy as sa


revision = "0033_absence_leave_review"
down_revision = "0032_ai_language_provenance"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("payroll_items")
    }
    if "absence_leave_reviewed" not in columns:
        op.add_column(
            "payroll_items",
            sa.Column(
                "absence_leave_reviewed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "absence_leave_review_fingerprint" not in columns:
        op.add_column(
            "payroll_items",
            sa.Column("absence_leave_review_fingerprint", sa.String(64)),
        )


def downgrade():
    pass
