"""Pending original-source discrepancy review receipt, no registration."""
from alembic import op
import sqlalchemy as sa
revision='0059_attendance_review_receipt'
down_revision='0058_policy_edit_preview'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('attendance_evidence_review_receipts',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),sa.Column('session_version',sa.Integer(),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('comparison_id',sa.String(64),sa.ForeignKey('department_fingerprint_preparations.id'),nullable=False),
        sa.Column('employee_id',sa.Integer(),sa.ForeignKey('employees.id'),nullable=False),
        sa.Column('work_date',sa.Date(),nullable=False),sa.Column('proposal_json',sa.Text(),nullable=False),
        sa.Column('evidence_json',sa.Text(),nullable=False),
        sa.Column('source_digest',sa.String(64),nullable=False),sa.Column('request_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))

    op.create_table('attendance_evidence_consumptions',
        sa.Column('receipt_id',sa.String(64),sa.ForeignKey('attendance_evidence_review_receipts.id'),primary_key=True),
        sa.Column('attendance_id',sa.Integer(),sa.ForeignKey('attendance_daily_records.id'),nullable=False,unique=True),
        sa.Column('attendance_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM attendance_evidence_review_receipts')).scalar_one():
        raise RuntimeError('Refusing downgrade: pending review evidence would be lost.')
    op.drop_table('attendance_evidence_consumptions')
    op.drop_table('attendance_evidence_review_receipts')
