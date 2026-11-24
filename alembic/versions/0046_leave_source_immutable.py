"""Protect only source-bound new-module ledger history from mutation.

Legacy rows retain their existing behavior. No posting/route/policy enabled.
"""
from alembic import op

revision = '0046_leave_source_immutable'
down_revision = '0045_leave_source_identity'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE FUNCTION prevent_leave_source_mutation() RETURNS trigger AS $$
    BEGIN
      IF OLD.source_operation_key IS NOT NULL THEN
        RAISE EXCEPTION 'source-bound leave history is immutable';
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql''')
    op.execute('''CREATE TRIGGER trg_leave_source_immutable BEFORE UPDATE OR DELETE
    ON leave_balance_transactions FOR EACH ROW EXECUTE FUNCTION prevent_leave_source_mutation()''')


def downgrade():
    if op.get_bind().exec_driver_sql(
            'SELECT count(*) FROM leave_balance_transactions WHERE source_operation_key IS NOT NULL').scalar_one():
        raise RuntimeError('Refusing downgrade: source-bound history would lose immutability.')
    op.execute('DROP TRIGGER trg_leave_source_immutable ON leave_balance_transactions')
    op.execute('DROP FUNCTION prevent_leave_source_mutation()')
