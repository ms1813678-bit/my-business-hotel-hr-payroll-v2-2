"""Exact month earned/held/released/usage receipts, no backfill."""
import sqlalchemy as sa
from alembic import op
revision='0071_leave_month_earning'
down_revision='0070_exact_leave_amount'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('leave_month_earnings',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('selection_id',sa.String(64),sa.ForeignKey('leave_opening_selections.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('month_start',sa.Date(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),
        sa.Column('session_version',sa.Integer(),nullable=False),
        sa.Column('restore_epoch',sa.String(128)),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('source_json',sa.Text(),nullable=False),
        sa.Column('result_json',sa.Text(),nullable=False),
        sa.Column('result_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('employee_id','month_start',name='uq_leave_month_earning_scope'),
        sa.CheckConstraint('EXTRACT(DAY FROM month_start) = 1',name='ck_leave_month_earning_first'))
    op.execute("""CREATE FUNCTION prevent_leave_month_earning_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'monthly earning receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE','DELETE','TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_leave_month_earning_{command.lower()} BEFORE {command} ON leave_month_earnings FOR EACH STATEMENT EXECUTE FUNCTION prevent_leave_month_earning_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_month_earnings')).scalar_one():
        raise RuntimeError('Refusing downgrade: immutable monthly earning evidence exists.')
    op.drop_table('leave_month_earnings')
    op.execute('DROP FUNCTION prevent_leave_month_earning_mutation()')
