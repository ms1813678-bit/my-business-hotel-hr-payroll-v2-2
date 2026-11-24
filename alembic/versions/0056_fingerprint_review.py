"""Bounded original fingerprint review evidence, no attendance effects."""
import sqlalchemy as sa
from alembic import op
revision='0056_fingerprint_review'
down_revision='0055_department_contract'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('department_fingerprint_preparations',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('return_id',sa.Integer(),sa.ForeignKey('department_workbook_preparations.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('fingerprint_bytes',sa.LargeBinary(),nullable=False),
        sa.Column('fingerprint_sha256',sa.String(64),nullable=False),
        sa.Column('review_json',sa.Text(),nullable=False),
        sa.Column('review_sha256',sa.String(64),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM department_fingerprint_preparations')).scalar_one():
        raise RuntimeError('Refusing downgrade: original fingerprint review evidence would be lost.')
    op.drop_table('department_fingerprint_preparations')
