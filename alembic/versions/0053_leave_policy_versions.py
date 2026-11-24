"""Explicit policy snapshots, no defaults silently activated or recalculation."""
import sqlalchemy as sa
from alembic import op

revision = '0053_leave_policy_versions'
down_revision = '0052_manual_opening_groups'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_policy_versions',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('policy_json',sa.Text(),nullable=False),
        sa.Column('policy_sha256',sa.String(64),nullable=False),
        sa.Column('reason',sa.Text(),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_policy_versions')).scalar_one():
        raise RuntimeError('Refusing downgrade: policy review evidence would be lost.')
    op.drop_table('leave_policy_versions')
