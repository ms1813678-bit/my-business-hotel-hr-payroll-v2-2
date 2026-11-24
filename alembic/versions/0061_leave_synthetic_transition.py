"""Synthetic-only local transition operation identity, no balance migration."""
from alembic import op
import sqlalchemy as sa
revision = '0061_leave_synthetic_transition'
down_revision = '0060_attendance_occurrence_claim'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_synthetic_transitions',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('scope_key', sa.String(64), nullable=False, unique=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id'), nullable=False),
        sa.Column('session_id', sa.String(128), nullable=False),
        sa.Column('session_version', sa.Integer(), nullable=False),
        sa.Column('restore_epoch', sa.Text()),
        sa.Column('request_digest', sa.String(64), nullable=False),
        sa.Column('result_json', sa.Text(), nullable=False),
        sa.Column('post_reconciliation_digest', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.execute("""CREATE FUNCTION refuse_synthetic_transition_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Synthetic transition history is immutable'; END $$""")
    op.execute('CREATE TRIGGER trg_leave_synthetic_transition_immutable BEFORE UPDATE OR DELETE ON leave_synthetic_transitions FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_transition_mutation()')
    op.create_table('leave_synthetic_transition_entries',
        sa.Column('operation_id', sa.String(64), sa.ForeignKey('leave_synthetic_transitions.id'), primary_key=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), primary_key=True),
        sa.Column('code', sa.String(1), primary_key=True),
        sa.Column('ledger_id', sa.Integer(), sa.ForeignKey('leave_balance_transactions.id'), unique=True),
        sa.Column('ledger_digest', sa.String(64)))
    op.execute('CREATE TRIGGER trg_leave_synthetic_entry_immutable BEFORE UPDATE OR DELETE ON leave_synthetic_transition_entries FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_transition_mutation()')
    op.execute("""CREATE FUNCTION refuse_synthetic_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF EXISTS (SELECT 1 FROM leave_synthetic_transition_entries WHERE ledger_id=OLD.id)
        THEN RAISE EXCEPTION 'Synthetic transition ledger history is immutable'; END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$""")
    op.execute('CREATE TRIGGER trg_leave_synthetic_ledger_immutable BEFORE UPDATE OR DELETE ON leave_balance_transactions FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_ledger_mutation()')



def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_synthetic_transitions')).scalar_one():
        raise RuntimeError('Refusing downgrade: synthetic transition history would be lost.')
    op.execute('DROP TRIGGER trg_leave_synthetic_ledger_immutable ON leave_balance_transactions')
    op.execute('DROP FUNCTION refuse_synthetic_ledger_mutation()')
    op.drop_table('leave_synthetic_transition_entries')
    op.drop_table('leave_synthetic_transitions')
    op.execute('DROP FUNCTION refuse_synthetic_transition_mutation()')
