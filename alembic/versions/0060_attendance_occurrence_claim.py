"""Prevent cross-receipt reuse of original raw fingerprint occurrences."""
from alembic import op
import sqlalchemy as sa
revision='0060_attendance_occurrence_claim'
down_revision='0059_attendance_review_receipt'
branch_labels=None
depends_on=None


def upgrade():
    # An existing unclaimed consumption cannot safely be reconstructed silently.
    if op.get_bind().execute(sa.text('SELECT count(*) FROM attendance_evidence_consumptions')).scalar_one():
        raise RuntimeError('Existing consumed receipts require explicit ordinal-claim reconciliation before migration.')
    op.create_table('attendance_evidence_occurrence_claims',
        sa.Column('fingerprint_sha256',sa.String(64),primary_key=True),
        sa.Column('ordinal',sa.Integer(),primary_key=True),
        sa.Column('receipt_id',sa.String(64),sa.ForeignKey('attendance_evidence_review_receipts.id'),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM attendance_evidence_occurrence_claims')).scalar_one():
        raise RuntimeError('Refusing downgrade: original occurrence consumption would be lost.')
    op.drop_table('attendance_evidence_occurrence_claims')
