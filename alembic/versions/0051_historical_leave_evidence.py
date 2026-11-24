"""Historical leave source proposals, not ledger imports or grants."""
import sqlalchemy as sa

from alembic import op

revision = '0051_historical_leave_evidence'
down_revision = '0050_workbook_manifest_digest'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_historical_evidence_preparations',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id'), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('proposal_json', sa.Text(), nullable=False),
        sa.Column('proposal_sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('restore_epoch', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_historical_evidence_preparations')).scalar_one():
        raise RuntimeError('Refusing downgrade: historical source evidence would be lost.')
    op.drop_table('leave_historical_evidence_preparations')
