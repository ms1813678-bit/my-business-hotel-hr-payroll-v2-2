"""Manual source-bound daily OT review and immutable award, no backfill/posting."""
from alembic import op
import sqlalchemy as sa
revision='0065_overtime_day_review'
down_revision='0064_reconciliation_approval'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('payroll_runs',sa.Column('overtime_daily_required',sa.Boolean(),nullable=False,server_default=sa.false()))
    op.alter_column('payroll_items','overtime_hours',type_=sa.Numeric(12,6),existing_type=sa.Numeric(8,2),existing_nullable=False)
    # Existing editable unpaid drafts must not keep a monthly-only bypass.
    # Closed/approved/paid snapshots are not recalculated or silently migrated.
    op.execute("UPDATE payroll_runs SET overtime_daily_required=true WHERE status IN ('draft','reopened')")
    op.add_column('payroll_items',sa.Column('overtime_daily_digest',sa.String(64)))
    op.add_column('payroll_items',sa.Column('overtime_daily_hours',sa.Text()))
    op.create_table('overtime_day_review_receipts',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('item_id',sa.Integer(),sa.ForeignKey('payroll_items.id'),nullable=False),
        sa.Column('work_date',sa.Date(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('session_id',sa.String(128),nullable=False),
        sa.Column('session_version',sa.Integer(),nullable=False),
        sa.Column('restore_epoch',sa.Text()),
        sa.Column('predecessor_id',sa.String(64)),
        sa.Column('proposal_json',sa.Text(),nullable=False),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('integrity_digest',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('id','item_id','work_date',name='uq_ot_receipt_scope'),
        sa.CheckConstraint('expires_at > created_at',name='ck_ot_receipt_expiry'))
    op.create_table('overtime_day_awards',
        sa.Column('receipt_id',sa.String(64),sa.ForeignKey('overtime_day_review_receipts.id'),primary_key=True),
        sa.Column('predecessor_id',sa.String(64),sa.ForeignKey('overtime_day_awards.receipt_id')),
        sa.Column('item_id',sa.Integer(),sa.ForeignKey('payroll_items.id'),nullable=False),
        sa.Column('work_date',sa.Date(),nullable=False),
        sa.Column('manual_hours',sa.Numeric(12,6),nullable=False),
        sa.Column('rate_divisor',sa.Integer(),nullable=False),
        sa.Column('source_digest',sa.String(64),nullable=False),
        sa.Column('amount_unrounded',sa.Text(),nullable=False),
        sa.Column('confirmed_by',sa.Integer(),sa.ForeignKey('admin_accounts.id'),nullable=False),
        sa.Column('confirmed_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('predecessor_id',name='uq_overtime_award_successor'),
        sa.ForeignKeyConstraint(['receipt_id','item_id','work_date'],['overtime_day_review_receipts.id','overtime_day_review_receipts.item_id','overtime_day_review_receipts.work_date'],name='fk_ot_award_receipt_scope'),
        sa.CheckConstraint('manual_hours >= 0 AND (manual_hours > 0 OR predecessor_id IS NOT NULL) AND rate_divisor IN (8,9,12)',name='ck_overtime_award_values'))
    op.create_foreign_key('fk_ot_receipt_predecessor','overtime_day_review_receipts','overtime_day_awards',['predecessor_id'],['receipt_id'])
    op.execute("""CREATE FUNCTION enforce_ot_award_scope() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'overtime_day_awards' THEN
        IF NOT EXISTS(SELECT 1 FROM overtime_day_review_receipts r WHERE r.id=NEW.receipt_id AND r.item_id=NEW.item_id AND r.work_date=NEW.work_date AND r.predecessor_id IS NOT DISTINCT FROM NEW.predecessor_id) THEN
          RAISE EXCEPTION 'Daily award receipt scope mismatch';
        END IF;
      END IF;
      IF NEW.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM overtime_day_awards a WHERE a.receipt_id=NEW.predecessor_id AND a.item_id=NEW.item_id AND a.work_date=NEW.work_date) THEN
        RAISE EXCEPTION 'Daily predecessor item/day mismatch';
      END IF;
      RETURN NEW;
    END $$""")
    for table in ('overtime_day_review_receipts','overtime_day_awards'):
        op.execute(f'CREATE TRIGGER trg_{table}_scope BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION enforce_ot_award_scope()')
    op.create_index('uq_overtime_award_baseline','overtime_day_awards',['item_id','work_date'],unique=True,postgresql_where=sa.text('predecessor_id IS NULL'))
    for table in ('overtime_day_review_receipts','overtime_day_awards'):
        op.execute(f'CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION refuse_synthetic_transition_mutation()')

def downgrade():
    if op.get_bind().execute(sa.text("SELECT count(*) FROM payroll_items WHERE overtime_hours != round(overtime_hours,2) OR overtime_hours>999999.99")).scalar_one():
        raise RuntimeError('Refusing downgrade: exact overtime hours would be rounded or overflow.')
    for table in ('overtime_day_awards','overtime_day_review_receipts'):
        if op.get_bind().execute(sa.text('SELECT count(*) FROM '+table)).scalar_one():
            raise RuntimeError('Refusing downgrade: overtime review history would be lost.')
    op.drop_constraint('fk_ot_receipt_predecessor','overtime_day_review_receipts',type_='foreignkey')
    op.drop_table('overtime_day_awards')
    op.drop_table('overtime_day_review_receipts')
    op.execute('DROP FUNCTION enforce_ot_award_scope()')
    op.drop_column('payroll_runs','overtime_daily_required')
    op.drop_column('payroll_items','overtime_daily_hours')
    op.drop_column('payroll_items','overtime_daily_digest')
    op.alter_column('payroll_items','overtime_hours',type_=sa.Numeric(8,2),existing_type=sa.Numeric(12,6),existing_nullable=False)
