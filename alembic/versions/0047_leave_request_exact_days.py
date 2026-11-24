"""Expand request day precision without changing stored values or records."""
import sqlalchemy as sa

from alembic import op

revision = '0047_leave_request_exact_days'
down_revision = '0046_leave_source_immutable'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('leave_records', 'charged_days', existing_type=sa.Numeric(7, 2),
                    type_=sa.Numeric(), existing_nullable=False)


def downgrade():
    count = op.get_bind().execute(sa.text('''SELECT count(*) FROM leave_records
        WHERE charged_days <> round(charged_days,2) OR abs(charged_days) >= 100000''')).scalar_one()
    if count:
        raise RuntimeError('Refusing request precision downgrade: values would round or overflow.')
    op.alter_column('leave_records', 'charged_days', existing_type=sa.Numeric(),
                    type_=sa.Numeric(7, 2), existing_nullable=False)
