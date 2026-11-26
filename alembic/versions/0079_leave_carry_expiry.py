"""Explicit reviewed expired-grant exclusion, never automatic erasure."""
from alembic import op
import sqlalchemy as sa
revision='0079_leave_carry_expiry'
down_revision='0078_leave_zero_decision'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('leave_carry_expiries',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('leave_type_id',sa.Integer(),sa.ForeignKey('leave_types.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('grant_kind',sa.String(16),nullable=False),sa.Column('grant_id',sa.String(64),nullable=False),
        sa.Column('ledger_id',sa.Integer(),sa.ForeignKey('leave_balance_transactions.id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('excluded_canonical',sa.Text(),nullable=False),
        sa.Column('source_digest',sa.String(64),nullable=False),sa.Column('request_json',sa.Text(),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),sa.Column('result_json',sa.Text(),nullable=False),
        sa.Column('result_digest',sa.String(64),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('grant_kind','grant_id',name='uq_carry_expiry_grant'),
        sa.CheckConstraint("grant_kind IN ('carry','annual')",name='ck_carry_expiry_kind'))
    op.execute("""CREATE FUNCTION prevent_carry_expiry_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'carry expiry receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE','DELETE','TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_carry_expiry_{command.lower()} BEFORE {command} ON leave_carry_expiries FOR EACH STATEMENT EXECUTE FUNCTION prevent_carry_expiry_mutation()')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_carry_expiries')).scalar_one():
        raise RuntimeError('Refusing downgrade: grant exclusion evidence exists.')
    op.drop_table('leave_carry_expiries')
    op.execute('DROP FUNCTION prevent_carry_expiry_mutation()')
