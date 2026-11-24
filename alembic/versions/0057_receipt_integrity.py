"""New verified receipt hashes; old evidence is not blindly authenticated."""
from alembic import op
import sqlalchemy as sa
revision='0057_receipt_integrity'
down_revision='0056_fingerprint_review'
branch_labels=None
depends_on=None


def upgrade():
    op.add_column('department_workbook_preparations',sa.Column('evidence_sha256',sa.String(64),nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM department_workbook_preparations WHERE evidence_sha256 IS NOT NULL')).scalar_one():
        raise RuntimeError('Refusing downgrade: verified receipt integrity would be lost.')
    op.drop_column('department_workbook_preparations','evidence_sha256')
