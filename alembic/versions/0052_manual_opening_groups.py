"""Explicit pending manual opening groups, no balance effects."""
import sqlalchemy as sa

from alembic import op

revision = '0052_manual_opening_groups'
down_revision = '0051_historical_leave_evidence'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_manual_opening_groups',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id'), nullable=False),
        *(sa.Column(field, sa.String(64), sa.ForeignKey('leave_historical_evidence_preparations.id'), nullable=False)
          for field in ('annual_id','weekly_id','holiday_id')),
        sa.Column('selection_sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('restore_epoch', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_manual_opening_groups')).scalar_one():
        raise RuntimeError('Refusing downgrade: manual opening group evidence would be lost.')
    op.drop_table('leave_manual_opening_groups')
