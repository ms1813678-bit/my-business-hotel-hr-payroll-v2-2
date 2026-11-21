"""add employee nationality and passport

Revision ID: 0027_employee_nationality
Revises: 0026_ai_conversation_messages
"""
from alembic import op
import sqlalchemy as sa


revision = "0027_employee_nationality"
down_revision = "0026_ai_conversation_messages"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    existing_columns = {column["name"] for column in _inspector().get_columns("employees")}
    if "nationality" not in existing_columns:
        op.add_column("employees", sa.Column("nationality", sa.String(10), nullable=True))
        op.execute("UPDATE employees SET nationality = 'egyptian' WHERE nationality IS NULL")
    if "passport_number" not in existing_columns:
        op.add_column("employees", sa.Column("passport_number", sa.String(20), nullable=True))
    existing_uniques = {item["name"] for item in _inspector().get_unique_constraints("employees")}
    if "uq_employees_passport_number" not in existing_uniques:
        op.create_unique_constraint("uq_employees_passport_number", "employees", ["passport_number"])


def downgrade():
    # Retain the columns: dropping them would discard recorded identity data.
    pass
