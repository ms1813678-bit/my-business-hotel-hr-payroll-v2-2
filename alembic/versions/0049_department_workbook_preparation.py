"""Unexposed server workbook evidence storage, no business data changes."""
import sqlalchemy as sa

from alembic import op

revision = '0049_department_workbooks'
down_revision = '0048_leave_review_receipts'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('department_workbook_exports',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id'), nullable=False),
        sa.Column('manifest_json', sa.Text(), nullable=False),
        sa.Column('source_digest', sa.String(64), nullable=False),
        sa.Column('restore_epoch', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('invalidated_at', sa.DateTime(timezone=True)))
    op.create_table('department_workbook_preparations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('export_id', sa.String(64), sa.ForeignKey('department_workbook_exports.id'), nullable=False),
        sa.Column('return_sha256', sa.String(64), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('admin_accounts.id'), nullable=False),
        sa.Column('evidence_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('export_id', 'return_sha256', name='uq_dept_workbook_preparation'))


def downgrade():
    for name in ('department_workbook_preparations', 'department_workbook_exports'):
        if op.get_bind().execute(sa.text('SELECT count(*) FROM ' + name)).scalar_one():
            raise RuntimeError('Refusing downgrade: workbook evidence would be lost.')
    op.drop_table('department_workbook_preparations')
    op.drop_table('department_workbook_exports')
