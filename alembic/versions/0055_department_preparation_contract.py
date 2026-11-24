"""Explicit confirmed export input contracts, no attendance registration."""
import sqlalchemy as sa
from alembic import op
revision='0055_department_contract'
down_revision='0054_leave_opening_selections'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('department_preparation_contracts',
        sa.Column('export_id',sa.String(64),sa.ForeignKey('department_workbook_exports.id'),primary_key=True),
        sa.Column('selection_json',sa.Text(),nullable=False),
        sa.Column('selection_sha256',sa.String(64),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM department_preparation_contracts')).scalar_one():
        raise RuntimeError('Refusing downgrade: department source contracts would be lost.')
    op.drop_table('department_preparation_contracts')
