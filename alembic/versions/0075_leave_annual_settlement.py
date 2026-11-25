"""Immutable annual cash-day and remaining-carry settlement, no payment."""
from alembic import op
import sqlalchemy as sa
revision = '0075_leave_annual_settlement'
down_revision = '0074_leave_cash_out'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_annual_settlements',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('leave_type_id', sa.Integer(), sa.ForeignKey('leave_types.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('source_year', sa.Integer(), nullable=False),
        sa.Column('cash_out_id', sa.String(64), sa.ForeignKey('leave_cash_outs.id', ondelete='RESTRICT')),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('result_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('employee_id', 'leave_type_id', 'source_year', name='uq_annual_settlement_year'))
    op.execute("""CREATE FUNCTION prevent_annual_settlement_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'annual settlement receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE', 'DELETE', 'TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_annual_settlement_{command.lower()} BEFORE {command} ON leave_annual_settlements FOR EACH STATEMENT EXECUTE FUNCTION prevent_annual_settlement_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_annual_settlements')).scalar_one():
        raise RuntimeError('Refusing downgrade: annual settlement evidence exists.')
    op.drop_table('leave_annual_settlements')
    op.execute('DROP FUNCTION prevent_annual_settlement_mutation()')
