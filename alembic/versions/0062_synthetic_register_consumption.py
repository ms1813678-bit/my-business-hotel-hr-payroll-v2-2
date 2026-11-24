"""Source-bound synthetic register consumption, no backfill or registration."""
from alembic import op
import sqlalchemy as sa
revision='0062_synthetic_register_consume'
down_revision='0061_leave_synthetic_transition'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('leave_synthetic_register_consumptions',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('comparison_id',sa.String(64),sa.ForeignKey('department_fingerprint_preparations.id'),nullable=False),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),
        sa.Column('session_version',sa.Integer(),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('result_json',sa.Text(),nullable=False),
        sa.UniqueConstraint('comparison_id','employee_id',name='uq_synthetic_register_comparison_employee'))
    op.execute('CREATE TRIGGER trg_leave_synthetic_register_immutable BEFORE UPDATE OR DELETE ON leave_synthetic_register_consumptions FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_transition_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_synthetic_register_consumptions')).scalar_one():
        raise RuntimeError('Refusing downgrade: register consumption evidence would be lost.')
    op.drop_table('leave_synthetic_register_consumptions')
