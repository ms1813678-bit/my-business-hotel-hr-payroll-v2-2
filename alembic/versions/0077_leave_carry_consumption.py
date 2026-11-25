"""Explicit grant attribution to existing monthly usage, no second debit."""
from alembic import op
import sqlalchemy as sa
revision='0077_leave_carry_consumption'
down_revision='0076_leave_year_transition'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('leave_carry_consumptions',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('leave_type_id',sa.Integer(),sa.ForeignKey('leave_types.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('grant_kind',sa.String(16),nullable=False),sa.Column('grant_id',sa.String(64),nullable=False),
        sa.Column('usage_ledger_id',sa.Integer(),sa.ForeignKey('leave_balance_transactions.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('work_date',sa.Date(),nullable=False),sa.Column('quantity_canonical',sa.Text(),nullable=False),
        sa.Column('source_digest',sa.String(64),nullable=False),sa.Column('request_json',sa.Text(),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),sa.Column('result_json',sa.Text(),nullable=False),
        sa.Column('result_digest',sa.String(64),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("grant_kind IN ('carry','annual')",name='ck_carry_consumption_kind'))
    op.execute("""CREATE FUNCTION prevent_carry_consumption_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'carry consumption receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE','DELETE','TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_carry_consumption_{command.lower()} BEFORE {command} ON leave_carry_consumptions FOR EACH STATEMENT EXECUTE FUNCTION prevent_carry_consumption_mutation()')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_carry_consumptions')).scalar_one():
        raise RuntimeError('Refusing downgrade: carry consumption evidence exists.')
    op.drop_table('leave_carry_consumptions')
    op.execute('DROP FUNCTION prevent_carry_consumption_mutation()')
