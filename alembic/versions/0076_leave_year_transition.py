"""Immutable completed-year transition checkpoint, no duplicate opening."""
from alembic import op
import sqlalchemy as sa
revision = '0076_leave_year_transition'
down_revision = '0075_leave_annual_settlement'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_year_transitions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('source_year', sa.Integer(), nullable=False),
        sa.Column('target_year', sa.Integer(), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('result_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('employee_id', 'source_year', name='uq_leave_year_transition_year'),
        sa.CheckConstraint('target_year = source_year + 1 AND source_year >= 1 AND source_year <= 9998', name='ck_leave_year_transition_adjacent'))
    op.execute("""CREATE FUNCTION prevent_year_transition_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'year transition receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE', 'DELETE', 'TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_year_transition_{command.lower()} BEFORE {command} ON leave_year_transitions FOR EACH STATEMENT EXECUTE FUNCTION prevent_year_transition_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_year_transitions')).scalar_one():
        raise RuntimeError('Refusing downgrade: year transition evidence exists.')
    op.drop_table('leave_year_transitions')
    op.execute('DROP FUNCTION prevent_year_transition_mutation()')
