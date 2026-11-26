"""Reviewed reversal + replacement correction of a posted leave month (Mo answer 2 of the 12-question batch, relayed by Main; design is a technical settlement)."""
from alembic import op
import sqlalchemy as sa
revision = '0083_leave_month_correction'
down_revision = '0082_payroll_carry_deduction'   # payroll carry lane; chain is 0081 leave cash, 0082 payroll carry, 0083 correction
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_month_corrections',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('month_start', sa.Date(), nullable=False),
        sa.Column('original_receipt_id', sa.String(64), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('session_id', sa.String(128), nullable=False), sa.Column('session_version', sa.Integer(), nullable=False),
        sa.Column('restore_epoch', sa.String(128)),
        sa.Column('source_digest', sa.String(64), nullable=False), sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('plan_json', sa.Text(), nullable=False), sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('result_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('original_receipt_id', 'sequence', name='uq_leave_month_correction_seq'),
        sa.CheckConstraint('sequence >= 1', name='ck_leave_month_correction_seq'))
    op.execute("""CREATE FUNCTION prevent_leave_month_correction_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'leave month correction receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE', 'DELETE', 'TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_leave_month_correction_{command.lower()} BEFORE {command} ON leave_month_corrections '
                   'FOR EACH STATEMENT EXECUTE FUNCTION prevent_leave_month_correction_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_month_corrections')).scalar_one():
        raise RuntimeError('Refusing downgrade: leave month correction evidence exists.')
    op.drop_table('leave_month_corrections')
    op.execute('DROP FUNCTION prevent_leave_month_correction_mutation()')
