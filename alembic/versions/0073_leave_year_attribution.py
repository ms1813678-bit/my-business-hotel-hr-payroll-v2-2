"""Immutable reviewed attribution of existing untagged ledger rows, no credits."""
from alembic import op
import sqlalchemy as sa
revision = '0073_leave_year_attribution'
down_revision = '0072_leave_carry_allocation'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_year_attributions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('ledger_id', sa.Integer(), sa.ForeignKey('leave_balance_transactions.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('session_id', sa.String(128), nullable=False),
        sa.Column('session_version', sa.Integer(), nullable=False),
        sa.Column('restore_epoch', sa.String(128)),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('result_digest', sa.String(64), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.execute("""CREATE FUNCTION prevent_leave_year_attribution_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'year attribution receipt is immutable'; END; $$ LANGUAGE plpgsql""")
    for command in ('UPDATE', 'DELETE', 'TRUNCATE'):
        op.execute(f'CREATE TRIGGER trg_leave_year_attribution_{command.lower()} BEFORE {command} ON leave_year_attributions FOR EACH STATEMENT EXECUTE FUNCTION prevent_leave_year_attribution_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_year_attributions')).scalar_one():
        raise RuntimeError('Refusing downgrade: reviewed year attribution exists.')
    op.drop_table('leave_year_attributions')
    op.execute('DROP FUNCTION prevent_leave_year_attribution_mutation()')
