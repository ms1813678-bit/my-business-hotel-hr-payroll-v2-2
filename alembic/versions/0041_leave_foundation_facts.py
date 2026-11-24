"""Add nullable leave foundation facts without changing historic behavior.

No inferred DOB/insurance, no new logins/roles, no converted leave code/balance,
no recalculation and no payroll/attendance writes. NULL preserves unknown data.

Revision ID: 0041_leave_foundation_facts
Revises: 0040_pending_credential_reset
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_leave_foundation_facts"
down_revision = "0040_pending_credential_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("birth_date", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("prior_insurance_years", sa.Numeric(8, 4), nullable=True))
    op.create_check_constraint("ck_employee_prior_insurance_nonnegative", "employees",
                               "prior_insurance_years IS NULL OR prior_insurance_years >= 0")
    op.add_column("leave_records", sa.Column("certificate_attached", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("leave_records", "certificate_attached")
    op.drop_constraint("ck_employee_prior_insurance_nonnegative", "employees", type_="check")
    op.drop_column("employees", "prior_insurance_years")
    op.drop_column("employees", "birth_date")
