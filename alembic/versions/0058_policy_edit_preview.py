"""HR policy edit preview, no active policy or recalculation mutation."""
from alembic import op
import sqlalchemy as sa
revision='0058_policy_edit_preview'
down_revision='0057_receipt_integrity'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('leave_policy_edit_drafts',sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('base_policy_id',sa.String(64),sa.ForeignKey('leave_policy_versions.id'),nullable=False),
        sa.Column('base_sha256',sa.String(64),nullable=False),sa.Column('policy_json',sa.Text(),nullable=False),
        sa.Column('reason',sa.Text(),nullable=False),sa.Column('restore_epoch',sa.Text()),
        sa.Column('review_sha256',sa.String(64),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('saved_policy_id',sa.String(64),sa.ForeignKey('leave_policy_versions.id')))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_policy_edit_drafts')).scalar_one():
        raise RuntimeError('Refusing downgrade: policy review evidence would be lost.')
    op.drop_table('leave_policy_edit_drafts')
