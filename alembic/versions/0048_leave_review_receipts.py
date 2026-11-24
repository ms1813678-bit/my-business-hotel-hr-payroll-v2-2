"""Server-side register human review receipts, no business row changes."""
import sqlalchemy as sa

from alembic import op

revision = '0048_leave_review_receipts'
down_revision = '0047_leave_request_exact_days'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('leave_register_review_receipts',
        sa.Column('token_hash', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id'), nullable=False),
        sa.Column('session_id', sa.String(64), nullable=False),
        sa.Column('session_version', sa.Integer(), nullable=False),
        sa.Column('proposal_json', sa.Text(), nullable=False),
        sa.Column('snapshot_json', sa.Text(), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True)))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM leave_register_review_receipts')).scalar_one():
        raise RuntimeError('Refusing downgrade: register review evidence would be lost.')
    op.drop_table('leave_register_review_receipts')
