"""Manual daily OT review/confirmation. Unrouted until monthly integration reviewed.

Preserves HR+finance payroll powers, never finance leave-ledger writes. Caller
commits atomically. No autoaward, source guessing or monthly aggregate change.
"""
import json,re
from datetime import UTC,date,datetime,timedelta
from decimal import Decimal,localcontext
from hashlib import sha256
from sqlalchemy import select,text,inspect
from app.department_workbook_guard import bounded_workbook_operation
from app.models import (AdminAccount,AdminSession,PayrollItem,PayrollRun,PayrollPayment,
    AttendanceDailyRecord,AttendanceImportBatch,LeaveMonthClose,OvertimeDayReviewReceipt,OvertimeDayAward,AuditLog)
from app.restore_lock import acquire_credential_write_lock,read_restore_epoch
from app.overtime_day_source import attendance_duration_diagnostic


def scalar(v):
    if isinstance(v,datetime):
        return v.astimezone(UTC).isoformat() if v.tzinfo is not None else v.isoformat()
    if isinstance(v,(date,Decimal)):return str(v)
    raise TypeError('Unsupported source scalar.')
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=True,default=scalar,allow_nan=False)
def now():return datetime.now(UTC)
def hash_value(v):return sha256(canonical(v).encode()).hexdigest()
def clean(db):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean explicit transaction required.')

@bounded_workbook_operation
def authority(db,auth,*,read_only=False):
    if set(auth)!={'actor_id','session_id','session_version','expected_epoch'}:raise ValueError('Exact identity required.')
    if (type(auth['actor_id']) is not int or auth['actor_id']<1 or type(auth['session_version']) is not int or auth['session_version']<0
        or not isinstance(auth['session_id'],str) or not 1<=len(auth['session_id'])<=64
        or (auth['expected_epoch'] is not None and not isinstance(auth['expected_epoch'],str))):raise ValueError('Exact typed identity required.')
    if read_only:
        from app.restore_lock import acquire_authenticated_read_lock
        acquire_authenticated_read_lock(db)
    else:acquire_credential_write_lock(db,expected_epoch=auth['expected_epoch'])
    actor=db.get(AdminAccount,auth['actor_id'],populate_existing=True);login=db.get(AdminSession,auth['session_id'],populate_existing=True)
    if (actor is None or login is None or login.admin_account_id!=actor.id or actor.role not in {'system_admin','finance'}
        or not actor.active or actor.must_change_password or actor.pending_credential_reset or actor.session_version!=auth['session_version']
        or read_restore_epoch(db)!=auth['expected_epoch']):raise ValueError('Current payroll operator session required.')
    return actor

def lock_scope(db):
    if db.get_bind().dialect.name!='postgresql':raise ValueError('Migrated PostgreSQL required.')
    db.execute(text('LOCK TABLE payroll_items,payroll_runs,payroll_payments,leave_month_close,attendance_daily_records,attendance_import_batches,overtime_day_review_receipts,overtime_day_awards IN SHARE ROW EXCLUSIVE MODE'))

def key(value):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None:raise ValueError('Exact operation identity required.')
    return value

def proposal(item_id,work_date,manual_hours,rate_divisor,reason,predecessor_id=None):
    if type(item_id) is not int or item_id<1 or type(work_date) is not date:raise ValueError('Exact item/day required.')
    if not isinstance(manual_hours,Decimal) or not manual_hours.is_finite() or not Decimal(0)<=manual_hours<=Decimal('999999.999999'):raise ValueError('Positive storage-bounded manual hours required; no amount rounded.')
    if manual_hours==0 and predecessor_id is None:raise ValueError('Zero hours only corrects a prior award, never a new award.')
    if manual_hours.as_tuple().exponent < -6:raise ValueError('At most six fractional hour digits; no rounding.')
    if type(rate_divisor) is not int or rate_divisor not in (8,9,12):raise ValueError('Explicit rate divisor required.')
    if not isinstance(reason,str) or len(reason)>2000:raise ValueError('Bounded reason required.')
    if predecessor_id is not None:
        key(predecessor_id)
        if not reason.strip():raise ValueError('Replacement requires explicit correction reason.')
    return dict(item_id=item_id,work_date=work_date.isoformat(),manual_hours=format(manual_hours,'f'),rate_divisor=rate_divisor,reason=reason.strip(),predecessor_id=predecessor_id)

def derive(db,p,*,editable=True):
    item=db.get(PayrollItem,p['item_id'],populate_existing=True)
    if item is None:raise ValueError('Payroll item missing.')
    run=db.get(PayrollRun,item.payroll_run_id,populate_existing=True);day=date.fromisoformat(p['work_date'])
    if run is None or not run.period_start<=day<=run.period_end:raise ValueError('Matching payroll period required.')
    if editable and run.status not in {'draft','reopened'}:raise ValueError('Editable matching payroll period required.')
    if editable and db.scalar(select(PayrollPayment.payroll_item_id).where(PayrollPayment.payroll_item_id==item.id).limit(1)) is not None:raise ValueError('Paid item refuses daily award.')
    if editable and db.scalar(select(LeaveMonthClose.id).where(LeaveMonthClose.year==day.year,LeaveMonthClose.month==day.month)) is not None:raise ValueError('Closed leave month refuses daily award.')
    row=db.scalar(select(AttendanceDailyRecord).where(AttendanceDailyRecord.employee_id==item.employee_id,AttendanceDailyRecord.work_date==day).execution_options(populate_existing=True))
    scheduled,extra,issues=attendance_duration_diagnostic(row,day)
    if issues or extra is None:raise ValueError('Approved consistent same-day attendance required.')
    hours=Decimal(p['manual_hours'])
    if hours>extra and not p['reason']:raise ValueError('Excess hours require explicit reason.')
    if any(not isinstance(v,Decimal) or not v.is_finite() or v<0 for v in (item.salary_basis,item.performance_bonus)):raise ValueError('Nonnegative stored pay basis required.')
    with localcontext() as ctx:
        ctx.prec=250
        rate=(item.salary_basis+item.performance_bonus)/Decimal(30)/Decimal(p['rate_divisor']);amount=rate*hours
    batches=[]
    for source_type,batch_id in (('program_report',row.program_import_batch_id),('fingerprint',row.fingerprint_import_batch_id)):
        if batch_id is None:continue
        batch=db.get(AttendanceImportBatch,batch_id,populate_existing=True)
        if batch is None or batch.source_type!=source_type or batch.status=='rejected' or not batch.period_start<=day<=batch.period_end:raise ValueError('Attendance batch no longer valid.')
        # Batch parsed->approved does not change evidence; rejection fails above.
        batches.append(dict(id=batch.id,sha256=batch.file_sha256,source_type=batch.source_type,start=batch.period_start,end=batch.period_end))
    # Exclude monthly OT aggregate/approval fields, so another distinct-day award
    # cannot stale a review. Bind ALL actual attendance fields and pay inputs.
    snapshot=dict(version='financial-source-v1',proposal=p,batches=batches,payroll=dict(item_id=item.id,run_id=run.id,employee_id=item.employee_id,basic=str(item.salary_basis),performance=str(item.performance_bonus),start=run.period_start,end=run.period_end),attendance={c.key:getattr(row,c.key) for c in inspect(AttendanceDailyRecord).columns})
    return item,run,extra,amount,hash_value(snapshot)

def receipt_hash(row):
    return hash_value({k:getattr(row,k) for k in ('id','item_id','work_date','actor_id','session_id','session_version','restore_epoch','predecessor_id','proposal_json','source_digest','created_at','expires_at')})

def latest_day_award(db,item_id,work_date):
    from sqlalchemy.orm import aliased
    successor=aliased(OvertimeDayAward)
    return db.scalar(select(OvertimeDayAward).where(OvertimeDayAward.item_id==item_id,OvertimeDayAward.work_date==work_date,
        ~select(successor.receipt_id).where(successor.predecessor_id==OvertimeDayAward.receipt_id).exists()).execution_options(populate_existing=True))

def own_receipt(db,receipt_id,actor,auth):
    row=db.get(OvertimeDayReviewReceipt,key(receipt_id),populate_existing=True)
    if row is None or (row.actor_id,row.session_id,row.session_version,row.restore_epoch)!=(actor.id,auth['session_id'],auth['session_version'],auth['expected_epoch']) or receipt_hash(row)!=row.integrity_digest:raise ValueError('Current intact own receipt required.')
    p=json.loads(row.proposal_json)
    validated=proposal(p['item_id'],date.fromisoformat(p['work_date']),Decimal(p['manual_hours']),p['rate_divisor'],p['reason'],p['predecessor_id'])
    if validated!=p or (p['item_id'],p['work_date'],p['predecessor_id'])!=(row.item_id,row.work_date.isoformat(),row.predecessor_id):raise ValueError('Receipt scope mismatch.')
    return row,p

def checked_award(db,row,p):
    old=db.get(OvertimeDayAward,row.id,populate_existing=True)
    if old and (old.item_id,old.work_date,old.predecessor_id,old.manual_hours,old.rate_divisor,old.source_digest,old.confirmed_by)!=(row.item_id,row.work_date,row.predecessor_id,Decimal(p['manual_hours']),p['rate_divisor'],row.source_digest,row.actor_id):raise ValueError('Award integrity mismatch.')
    if old:
        try:amount=Decimal(old.amount_unrounded)
        except Exception as exc:raise ValueError('Award amount malformed.') from exc
        if not amount.is_finite() or amount<0:raise ValueError('Award amount malformed.')
    return old

@bounded_workbook_operation
def prepare_day_award(db,*,operation_id,item_id,work_date,manual_hours,rate_divisor,reason,predecessor_id=None,**auth):
    clean(db);actor=authority(db,auth);lock_scope(db);key(operation_id)
    p=proposal(item_id,work_date,manual_hours,rate_divisor,reason,predecessor_id)
    old=db.get(OvertimeDayReviewReceipt,operation_id,populate_existing=True)
    if old:
        old,stored=own_receipt(db,operation_id,actor,auth)
        if stored!=p:raise ValueError('Operation identity changed.')
        if checked_award(db,old,p):return old # historical replay, no new award or current-state authorization
    item,run,extra,amount,source=derive(db,p)
    latest=latest_day_award(db,item_id,work_date)
    if (latest.receipt_id if latest else None)!=predecessor_id:raise ValueError('Day already awarded or correction predecessor stale; review latest.')
    if old:
        if old.source_digest!=source or old.expires_at<=now():raise ValueError('Operation identity stale or expired.')
        return old
    created=now()
    row=OvertimeDayReviewReceipt(id=operation_id,item_id=item_id,work_date=work_date,actor_id=actor.id,session_id=auth['session_id'],session_version=auth['session_version'],restore_epoch=auth['expected_epoch'],predecessor_id=predecessor_id,proposal_json=canonical(p),source_digest=source,created_at=created,expires_at=created+timedelta(minutes=10))
    row.integrity_digest=receipt_hash(row);db.add(row);db.flush();return row

@bounded_workbook_operation
def confirm_day_award(db,*,receipt_id,human_confirmed,**auth):
    clean(db);actor=authority(db,auth);lock_scope(db)
    if human_confirmed is not True:raise ValueError('Explicit human confirmation required.')
    row,p=own_receipt(db,receipt_id,actor,auth)
    old=checked_award(db,row,p)
    if old:return old # Historical idempotent result, never repost/recalculate after close/payment.
    item,run,extra,amount,source=derive(db,p)
    if source!=row.source_digest:raise ValueError('Source changed; review again.')
    if row.expires_at<=now():raise ValueError('Review expired.')
    latest=latest_day_award(db,row.item_id,row.work_date)
    if (latest.receipt_id if latest else None)!=row.predecessor_id:raise ValueError('Day already awarded or correction predecessor stale; no overwrite.')
    award=OvertimeDayAward(receipt_id=row.id,predecessor_id=row.predecessor_id,item_id=row.item_id,work_date=row.work_date,manual_hours=Decimal(p['manual_hours']),rate_divisor=p['rate_divisor'],source_digest=source,amount_unrounded=str(amount),confirmed_by=actor.id,confirmed_at=now())
    db.add(award);db.add(AuditLog(actor=actor.username,action='overtime_day_awarded',entity_type='overtime_day_award',entity_id=row.id,new_value=canonical(dict(proposal=p,source_digest=source,amount_unrounded=str(amount),monthly_posted=False)),reason=p['reason'] or 'Explicit same-day manual overtime review'))
    db.flush();return award


def current_awards(db,item_id):
    from sqlalchemy.orm import aliased
    successor=aliased(OvertimeDayAward)
    return db.scalars(select(OvertimeDayAward).where(OvertimeDayAward.item_id==item_id,
        ~select(successor.receipt_id).where(successor.predecessor_id==OvertimeDayAward.receipt_id).exists()).order_by(OvertimeDayAward.work_date)).all()

def verified_day_totals(db,item_id):
    """No money rounding/policy choice here. Caller must hold payroll/source locks.

    Separates frozen financial-source verification from state transitions.
    Does not authorize postings, edits or payments.
    """
    hours=Decimal(0);amount=Decimal(0);parts=[]
    with localcontext() as ctx:
        ctx.prec=250
        for award in current_awards(db,item_id):
            row=db.get(OvertimeDayReviewReceipt,award.receipt_id,populate_existing=True)
            if row is None or receipt_hash(row)!=row.integrity_digest:raise ValueError('Stored daily review integrity mismatch.')
            p=json.loads(row.proposal_json)
            checked_award(db,row,p)
            item,run,extra,current,source=derive(db,p,editable=False)
            if source!=award.source_digest or current!=Decimal(award.amount_unrounded):raise ValueError('Daily overtime source stale; review replacement required.')
            hours+=award.manual_hours;amount+=current
            parts.append(dict(id=award.receipt_id,source=source,amount=award.amount_unrounded,hours=str(award.manual_hours)))
    return hours,amount,hash_value(dict(version='daily-aggregate-v1',awards=parts))


def stage_monthly_totals(db,item_id,*,rounding_contract):
    """Monthly integration helper. Explicit contract required.

    Caller holds current authority + lock_scope in one clean transaction. No
    default selects financial rounding policy. Actor-confirmed day awards are
    the only source; no monthly hand-entered hours or same-day double counting.
    """
    from app.payroll_math import money
    from app.payroll_workflow import recompute_payroll_item
    if rounding_contract!='existing-monthly-half-up':raise ValueError('Confirmed monthly rounding contract required.')
    item=db.get(PayrollItem,item_id,populate_existing=True)
    run=db.get(PayrollRun,item.payroll_run_id,populate_existing=True) if item else None
    if run is None or run.status not in {'draft','reopened'}:raise ValueError('Editable payroll required.')
    if db.scalar(select(PayrollPayment.id).where(PayrollPayment.payroll_item_id==item_id).limit(1)) is not None:raise ValueError('Paid payroll snapshot immutable.')
    hours,amount,digest=verified_day_totals(db,item_id)
    # Numeric legacy summary is widened by migration, never round manual hours.
    if hours>Decimal('999999.999999'):raise ValueError('Monthly hours exceed exact storage capacity.')
    rounded=money(amount)
    if rounded>Decimal('9999999999.99'):raise ValueError('Monthly money exceeds storage capacity.')
    item.overtime_daily_digest=digest;item.overtime_daily_hours=str(hours)
    item.overtime_hours=hours;item.overtime_amount=rounded
    awards=current_awards(db,item_id)
    item.overtime_approved=bool(awards)
    if awards:
        last=max(awards,key=lambda a:a.confirmed_at)
        actor=db.get(AdminAccount,last.confirmed_by)
        item.overtime_approved_by=actor.username if actor else None
        item.overtime_approved_at=last.confirmed_at
    else:item.overtime_approved_by=None;item.overtime_approved_at=None
    recompute_payroll_item(item,run)
    return item


def verify_monthly_snapshot(db,item):
    """Requires caller-held source/payroll locks; never mutates closed/pay data."""
    if item.overtime_daily_digest is None:
        if current_awards(db,item.id):raise ValueError('Daily awards not yet reviewed into monthly payroll.')
        if item.overtime_hours or item.overtime_amount or item.overtime_approved:raise ValueError('Monthly-only overtime requires daily review.')
        return
    hours,amount,digest=verified_day_totals(db,item.id)
    if digest!=item.overtime_daily_digest or hours!=Decimal(item.overtime_daily_hours):raise ValueError('Monthly daily overtime snapshot stale.')
    from app.payroll_math import money
    if item.overtime_hours!=hours or item.overtime_amount!=money(amount):raise ValueError('Monthly daily overtime amount mismatch.')


@bounded_workbook_operation
def monthly_lock_scope(db):
    """Global order before payroll row locks, shared with day confirmation.

    Existing endpoints enforce live role/session. This is serialization only,
    not a substitute for caller authorization or restore epoch validation.
    """
    if db.get_bind().dialect.name=='postgresql':
        from app.restore_lock import acquire_authenticated_read_lock
        acquire_authenticated_read_lock(db)
        lock_scope(db)


def item_has_payment(db,item_id):
    return db.scalar(select(PayrollPayment.id).where(PayrollPayment.payroll_item_id==item_id).limit(1)) is not None


def verify_frozen_monthly_snapshot(db,item):
    """Closed monthly proof, no mutable attendance/pay-source re-evaluation.

    Existing immutable day receipts + current append-only award heads must
    exactly reproduce the frozen aggregate. Never changes payable amounts.
    """
    if item.overtime_daily_digest is None:
        run=db.get(PayrollRun,item.payroll_run_id)
        if run.overtime_daily_required and not item_has_payment(db,item.id):verify_monthly_snapshot(db,item)
        return # Only historical frozen monthly snapshots may lack daily proof.
    parts=[];hours=Decimal(0);amount=Decimal(0)
    with localcontext() as ctx:
        ctx.prec=250
        for award in current_awards(db,item.id):
            row=db.get(OvertimeDayReviewReceipt,award.receipt_id,populate_existing=True)
            if row is None or receipt_hash(row)!=row.integrity_digest:raise ValueError('Frozen review integrity mismatch.')
            p=json.loads(row.proposal_json);checked_award(db,row,p)
            hours+=award.manual_hours;amount+=Decimal(award.amount_unrounded)
            parts.append(dict(id=award.receipt_id,source=award.source_digest,amount=award.amount_unrounded,hours=str(award.manual_hours)))
    digest=hash_value(dict(version='daily-aggregate-v1',awards=parts))
    from app.payroll_math import money
    if digest!=item.overtime_daily_digest or hours!=Decimal(item.overtime_daily_hours) or hours!=item.overtime_hours or money(amount)!=item.overtime_amount:raise ValueError('Frozen monthly overtime proof mismatch.')
