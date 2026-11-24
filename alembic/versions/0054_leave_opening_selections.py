"""Confirmed explicit manual source selections, no ledger postings."""
import sqlalchemy as sa
from alembic import op
revision = '0054_leave_opening_selections'
down_revision = '0053_leave_policy_versions'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_opening_selections',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id'),nullable=False),
        sa.Column('group_id',sa.String(64),sa.ForeignKey('leave_manual_opening_groups.id'),nullable=False),
        sa.Column('policy_id',sa.String(64),sa.ForeignKey('leave_policy_versions.id'),nullable=False),
        sa.Column('previous_id',sa.String(64),sa.ForeignKey('leave_opening_selections.id')),
        sa.Column('revision',sa.Integer(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('source_sha256',sa.String(64),nullable=False),
        sa.Column('reason',sa.Text(),nullable=False),sa.Column('restore_epoch',sa.Text()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('employee_id','revision',name='uq_leave_opening_selection_revision'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_opening_selections')).scalar_one():
        raise RuntimeError('Refusing downgrade: confirmed source history would be lost.')
    op.drop_table('leave_opening_selections')
