"""Immutable exact carry/zero source-year allocation receipts, no backfill."""
from alembic import op
import sqlalchemy as sa
revision='0072_leave_carry_allocation'
down_revision='0071_leave_month_earning'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('leave_carry_allocations',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('leave_type_id',sa.Integer(),sa.ForeignKey('leave_types.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('source_year',sa.Integer(),nullable=False),
        sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('result_digest',sa.String(64),nullable=False),
        sa.Column('request_json',sa.Text(),nullable=False),
        sa.Column('result_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('employee_id','leave_type_id','source_year',name='uq_leave_carry_source_year'),
        sa.CheckConstraint('source_year BETWEEN 1 AND 9998',name='ck_leave_carry_scope'))
    op.execute("""CREATE FUNCTION prevent_leave_carry_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'carry allocation receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE','DELETE','TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_leave_carry_{command.lower()} BEFORE {command} ON leave_carry_allocations FOR EACH STATEMENT EXECUTE FUNCTION prevent_leave_carry_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_carry_allocations')).scalar_one():
        raise RuntimeError('Refusing downgrade: carry allocation evidence exists.')
    op.drop_table('leave_carry_allocations')
    op.execute('DROP FUNCTION prevent_leave_carry_mutation()')
