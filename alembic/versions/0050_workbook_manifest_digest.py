"""Independent canonical manifest checksum, defense in depth, not signing."""
import hashlib

import sqlalchemy as sa

from alembic import op

revision = '0050_workbook_manifest_digest'
down_revision = '0049_department_workbooks'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('department_workbook_exports', sa.Column('manifest_sha256', sa.String(64)))
    bind = op.get_bind()
    for row in bind.execute(sa.text('SELECT id, manifest_json FROM department_workbook_exports')).mappings():
        bind.execute(sa.text('UPDATE department_workbook_exports SET manifest_sha256=:digest WHERE id=:id'),
                     {'id': row['id'], 'digest': hashlib.sha256(row['manifest_json'].encode()).hexdigest()})
    op.alter_column('department_workbook_exports', 'manifest_sha256', nullable=False)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM department_workbook_exports')).scalar_one():
        raise RuntimeError('Refusing downgrade: manifest integrity evidence would be lost.')
    op.drop_column('department_workbook_exports', 'manifest_sha256')
