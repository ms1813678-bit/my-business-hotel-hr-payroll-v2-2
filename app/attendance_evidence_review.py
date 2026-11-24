"""Pending explicit raw-occurrence review, then guarded pending attendance registration.

No automatic direction, daily approval, ledger/payroll or month-close effects.
Caller owns transaction and must commit/rollback this operation atomically.
No HTTP surface. Original bytes are reparsed each time. Global credential/restore
exclusive lock precedes operation key, strongest attendance locks, then source
SHARE readers. Do not compose after weaker table locks or unrelated writes.
Authenticated HTTP cookie expiry remains in SessionMiddleware (8 hours); this
internal API receives only resolved current identity, not a cookie. Current live
session existence/version/role/epoch is checked under the exclusive gate.
"""
import json,re
from datetime import date,datetime,UTC
from hashlib import sha256
from sqlalchemy import select,text,inspect
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.shift_day_store import read_saved_shift_days
from app.shift_evidence_source import preview_configured_shift_evidence
from app.models import AttendanceEvidenceReviewReceipt,AttendanceDailyRecord,AttendanceMonth,LeaveMonthClose,AuditLog,AttendanceEvidenceConsumption,PayrollRun,AttendanceEvidenceOccurrenceClaim,EmployeeServicePeriod
from app.time_semantics import local_elapsed_minutes_detail


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(value):return sha256(canonical(value).encode()).hexdigest()


def _identity(db,identity):
    if (set(identity)!={'actor_id','session_id','session_version','expected_epoch'}
        or type(identity['actor_id']) is not int or identity['actor_id']<1
        or type(identity['session_version']) is not int or identity['session_version']<0
        or not isinstance(identity['session_id'],str) or not 1<=len(identity['session_id'])<=64
        or (identity['expected_epoch'] is not None and not isinstance(identity['expected_epoch'],str))):
        raise ValueError('Exact current actor/session/epoch required.')
    return _guard(db,**identity)


def _derive(db,proposal,identity,expected_record_id=None,expected_receipt_id=None):
    # Exclusive restore gate already held, strongest attendance lock before source readers.
    db.execute(text('LOCK TABLE attendance_daily_records,attendance_months,leave_month_close,payroll_runs,attendance_evidence_occurrence_claims,employee_service_periods IN SHARE ROW EXCLUSIVE MODE'))
    source=read_saved_shift_days(db,comparison_id=proposal['comparison_id'],employee_id=proposal['employee_id'],**identity)
    day=date.fromisoformat(proposal['work_date'])
    if not any(v['date']==day.isoformat() and v['code']=='P' for v in source.days):
        raise ValueError('Only explicit saved department P is supported by this bridge.')
    windows=tuple((date.fromisoformat(d),i) for d,i in proposal['windows'])
    comparison=preview_configured_shift_evidence(db,employee_id=source.employee_id,work_date=day,selected_windows=windows,
        fingerprint_bytes=source.fingerprint_bytes,fingerprint_filename=source.filename_hint,operational_parse=True,**identity)
    _identity(db,identity)
    if db.scalar(select(LeaveMonthClose).where(LeaveMonthClose.year==day.year,LeaveMonthClose.month==day.month)) is not None:
        raise ValueError('Closed leave month cannot be changed by attendance registration.')
    months=db.scalars(select(AttendanceMonth).where(AttendanceMonth.period_start<=day,AttendanceMonth.period_end>=day).order_by(AttendanceMonth.id).execution_options(populate_existing=True)).all()
    if len(months)!=1 or months[0].status!='under_review':raise ValueError('Exactly one under-review attendance month required.')
    service=db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==source.employee_id,EmployeeServicePeriod.start_date<=day,
        (EmployeeServicePeriod.end_date.is_(None) | (EmployeeServicePeriod.end_date>=day))).order_by(EmployeeServicePeriod.id).execution_options(populate_existing=True)).all()
    if not service:raise ValueError('Employee must be in recorded service on workday.')
    payroll=db.scalars(select(PayrollRun).where(PayrollRun.period_start<=day,PayrollRun.period_end>=day).order_by(PayrollRun.id).execution_options(populate_existing=True)).all()
    if any(m.status in {'approved','closed'} for m in payroll):raise ValueError('Approved/closed payroll period cannot receive attendance changes.')
    existing=db.scalar(select(AttendanceDailyRecord).where(AttendanceDailyRecord.employee_id==source.employee_id,AttendanceDailyRecord.work_date==day).execution_options(populate_existing=True))
    if existing is not None and existing.id!=expected_record_id:raise ValueError('Existing attendance requires a separate reconciliation, never overwrite.')
    blocked={'unreadable_source_evidence','schedule_duration_uncertain'}
    if comparison.employee_code!=source.employee_code or comparison.employee_name!=source.employee_name:raise ValueError('Export/live employee identity changed; regenerate source.')
    if blocked.intersection(comparison.preview.issues) or comparison.preview.unresolved_identity_occurrences:
        raise ValueError('Unresolved or unreadable source needs identity/evidence review first.')
    occurrences={v['ordinal']:v for v in comparison.preview.occurrences}
    selected=[]
    for ordinal in proposal['ordinals']:
        if ordinal not in occurrences:raise ValueError('Selected ordinal not employee-linked within configured windows.')
        selected.append(occurrences[ordinal])
    raw_hash=sha256(source.fingerprint_bytes).hexdigest()
    claims=db.scalars(select(AttendanceEvidenceOccurrenceClaim).where(AttendanceEvidenceOccurrenceClaim.fingerprint_sha256==raw_hash,AttendanceEvidenceOccurrenceClaim.ordinal.in_(proposal['ordinals']))).all()
    if any(v.receipt_id!=expected_receipt_id for v in claims):raise ValueError('Original occurrence already used by another attendance review.')
    start,end=(v['raw_event']['punch_at'] for v in selected)
    if end<=start:raise ValueError('Explicit IN then OUT must be strictly ordered.')
    target=next((w for w in comparison.preview.windows if w['selection_id']==proposal['selected_window']),None)
    if target is None or target['work_date']!=day.isoformat():raise ValueError('Explicit target-workday configured window required.')
    if any(target['selection_id'] not in v['candidate_window_ids'] for v in selected):raise ValueError('Both selected occurrences must belong to explicit target window.')
    minutes,basis=local_elapsed_minutes_detail(start,end)
    if basis!='exact' or minutes<=0:raise ValueError('Uncertain elapsed duration cannot be registered.')
    # Review explicitly chooses direction; no first/last or duplicate deletion.
    payload=dict(fingerprint_sha256=raw_hash,saved_source=source.source_digest,configured_source=comparison.source_digest,proposal=proposal,
        configured_windows=[dict(w) for w in comparison.preview.windows],
        selected_occurrences=[dict(ordinal=v['ordinal'],raw_event={k:(x.isoformat() if isinstance(x,(date,datetime)) else x) for k,x in v['raw_event'].items()},candidate_window_ids=list(v['candidate_window_ids'])) for v in selected],
        issues=list(comparison.preview.issues),retained_occurrence_count=len(comparison.preview.occurrences),
        service_snapshots=[{c.key:str(getattr(m,c.key)) for c in inspect(EmployeeServicePeriod).columns} for m in service],
        payroll_snapshots=[{c.key:str(getattr(m,c.key)) for c in inspect(PayrollRun).columns} for m in payroll],
        month_snapshots=[{c.key:str(getattr(m,c.key)) for c in inspect(AttendanceMonth).columns} for m in months],attendance_existing=None)
    return digest(payload),start,end,minutes,target,canonical(payload)


@bounded_workbook_operation
def prepare_evidence_review(db,*,operation_id,comparison_id,employee_id,work_date,windows,selected_window,in_ordinal,out_ordinal,reason,**identity):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean explicit transaction required.')
    actor=_identity(db,identity)
    if type(employee_id) is not int or employee_id<1:raise ValueError('Exact positive employee id required.')
    if not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None:raise ValueError('Explicit unique operation id required.')
    if type(work_date) is not date or any(type(v) is not int or v<0 for v in (in_ordinal,out_ordinal)) or in_ordinal==out_ordinal:raise ValueError('Explicit date and distinct raw ordinals required.')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:raise ValueError('Explicit bounded review reason required.')
    if not isinstance(windows,(tuple,list)) or not 1<=len(windows)<=100:raise ValueError('Bounded explicit windows required.')
    if any(not isinstance(v,(tuple,list)) or len(v)!=2 or type(v[0]) is not date or type(v[1]) is not int for v in windows):raise ValueError('Exact configured date/id pairs required.')
    if not isinstance(selected_window,str) or len(selected_window)>200:raise ValueError('Explicit target window required.')
    proposal=dict(comparison_id=comparison_id,employee_id=employee_id,work_date=work_date.isoformat(),windows=[(d.isoformat(),i) for d,i in windows],
        selected_window=selected_window,ordinals=[in_ordinal,out_ordinal],reason=reason.strip())
    # Serialize idempotency before reads so concurrent same-id requests settle once.
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int(operation_id[:15],16)})
    _identity(db,identity)
    existing=db.get(AttendanceEvidenceReviewReceipt,operation_id,populate_existing=True)
    request=digest(dict(proposal=proposal,actor_id=actor.id,session_id=identity['session_id'],session_version=identity['session_version'],epoch=identity['expected_epoch']))
    if existing is not None:
        if (existing.actor_id,existing.session_id,existing.session_version,existing.restore_epoch,existing.comparison_id,existing.employee_id,existing.work_date)!=(actor.id,identity['session_id'],identity['session_version'],identity['expected_epoch'],comparison_id,employee_id,work_date):raise ValueError('Stored review identity mismatch.')
        if existing.request_digest!=request:raise ValueError('Operation id reused with changed request.')
        consumed=db.get(AttendanceEvidenceConsumption,operation_id,populate_existing=True)
        if consumed:
            consume_evidence_review(db,receipt_id=operation_id,confirmed_request_digest=request,**identity)
            return existing
        # Pending replay still revalidates facts, not only request equality.
        current,*_= _derive(db,proposal,identity)
        if sha256(existing.evidence_json.encode()).hexdigest()!=existing.source_digest:raise ValueError('Stored evidence integrity mismatch.')
        if existing.proposal_json!=canonical(proposal):raise ValueError('Stored proposal integrity mismatch.')
        if current!=existing.source_digest:raise ValueError('Saved review became stale.')
        return existing
    source_digest,_,_,_,_,evidence_json=_derive(db,proposal,identity)
    row=AttendanceEvidenceReviewReceipt(id=operation_id,actor_id=actor.id,session_id=identity['session_id'],session_version=identity['session_version'],
        restore_epoch=identity['expected_epoch'],comparison_id=comparison_id,employee_id=employee_id,work_date=work_date,
        proposal_json=canonical(proposal),evidence_json=evidence_json,source_digest=source_digest,request_digest=request,created_at=datetime.now(UTC))
    db.add(row);db.flush()
    db.add(AuditLog(actor=actor.username,action='attendance_evidence_review_prepared',entity_type='attendance_evidence_review_receipt',entity_id=row.id,
        new_value=canonical(dict(source_digest=source_digest,request_digest=request,registration_allowed=False)),reason=reason.strip()))
    db.flush();return row


def _record_digest(record):
    def value(v):
        if isinstance(v,datetime) and v.tzinfo is not None:return v.astimezone(UTC).isoformat()
        return str(v)
    return digest({c.key:value(getattr(record,c.key)) for c in inspect(AttendanceDailyRecord).columns})


@bounded_workbook_operation
def consume_evidence_review(db,*,receipt_id,confirmed_request_digest,route_comparison_id=None,route_employee_id=None,**identity):
    """Register only explicitly reviewed raw endpoints, still needs_review.

    Explicit digest confirmation binds the exact preview, not implicit consent.
    No selection inferred from first/last, previous-day, or schedule boundaries.
    """
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    actor=_identity(db,identity)
    if not isinstance(receipt_id,str) or re.fullmatch('[0-9a-f]{64}',receipt_id) is None:raise ValueError('Exact receipt required.')
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int(receipt_id[:15],16)})
    row=db.get(AttendanceEvidenceReviewReceipt,receipt_id,populate_existing=True)
    if row is None or (row.actor_id,row.session_id,row.session_version,row.restore_epoch)!=(actor.id,identity['session_id'],identity['session_version'],identity['expected_epoch']):raise ValueError('Current same-session review receipt required.')
    if not isinstance(row.proposal_json,str) or len(row.proposal_json.encode())>100000:raise ValueError('Bounded stored proposal required.')
    if route_comparison_id is not None and (row.comparison_id,row.employee_id)!=(route_comparison_id,route_employee_id):raise ValueError('Exact route-bound receipt required.')
    try:proposal=json.loads(row.proposal_json)
    except (TypeError,ValueError) as exc:raise ValueError('Malformed stored review.') from exc
    if type(proposal) is not dict or set(proposal)!={'comparison_id','employee_id','work_date','windows','selected_window','ordinals','reason'}:raise ValueError('Malformed stored review.')
    if (type(proposal['employee_id']) is not int or not isinstance(proposal['work_date'],str)
        or not isinstance(proposal['comparison_id'],str) or not isinstance(proposal['selected_window'],str)
        or not isinstance(proposal['reason'],str) or not 1<=len(proposal['reason'].strip())<=2000
        or type(proposal['ordinals']) is not list or len(proposal['ordinals'])!=2
        or any(type(v) is not int or v<0 for v in proposal['ordinals'])
        or type(proposal['windows']) is not list or not 1<=len(proposal['windows'])<=100
        or any(type(v) is not list or len(v)!=2 or not isinstance(v[0],str) or type(v[1]) is not int for v in proposal['windows'])):
        raise ValueError('Malformed stored review types.')
    if (proposal['comparison_id'],proposal['employee_id'],proposal['work_date'])!=(row.comparison_id,row.employee_id,row.work_date.isoformat()):raise ValueError('Receipt identity mismatch.')
    request=digest(dict(proposal=proposal,actor_id=actor.id,session_id=row.session_id,session_version=row.session_version,epoch=row.restore_epoch))
    if request!=row.request_digest or request!=confirmed_request_digest:raise ValueError('Exact unchanged explicit review confirmation required.')
    consumed=db.get(AttendanceEvidenceConsumption,receipt_id,populate_existing=True)
    current,start,end,minutes,target,evidence_json=_derive(db,proposal,identity,consumed.attendance_id if consumed else None,row.id)
    if row.evidence_json!=evidence_json:raise ValueError('Stored review evidence integrity mismatch.')
    if current!=row.source_digest:raise ValueError('Evidence/configuration/period changed; review again.')
    if consumed:
        record=db.get(AttendanceDailyRecord,consumed.attendance_id,populate_existing=True)
        if record is None or _record_digest(record)!=consumed.attendance_digest:raise ValueError('Registered attendance changed; separate reconciliation required.')
        return record
    now=datetime.now(UTC)
    month=json.loads(evidence_json)['month_snapshots'][0]
    record=AttendanceDailyRecord(employee_id=row.employee_id,work_date=row.work_date,
        program_import_batch_id=int(month['program_import_batch_id']) if month['program_import_batch_id']!='None' else None,
        fingerprint_import_batch_id=int(month['fingerprint_import_batch_id']) if month['fingerprint_import_batch_id']!='None' else None,
        manual_clock_in=start,manual_clock_out=end,manual_entry_reason=proposal['reason'],manual_entered_by=actor.username,
        manual_entered_at=now,worked_minutes=minutes,status='needs_review',
        review_notes=canonical(dict(receipt_id=row.id,source_digest=row.source_digest,ordinals=proposal['ordinals'],
            selected_window=proposal['selected_window'],schedule_not_assignment=True,unselected_occurrences_retained=True,
            no_final_approval=True,no_overtime_award=True)))
    # Do not set scheduled/approved/program/first/last fields from candidate windows.
    db.add(record);db.flush()
    for ordinal in proposal['ordinals']:
        db.add(AttendanceEvidenceOccurrenceClaim(fingerprint_sha256=json.loads(evidence_json)['fingerprint_sha256'],ordinal=ordinal,receipt_id=row.id))
    db.add(AttendanceEvidenceConsumption(receipt_id=row.id,attendance_id=record.id,attendance_digest=_record_digest(record),created_at=now))
    db.add(AuditLog(actor=actor.username,action='attendance_evidence_registered_pending',entity_type='attendance_daily_record',entity_id=str(record.id),
        new_value=canonical(dict(receipt_id=row.id,source_digest=row.source_digest,request_digest=request,status='needs_review')),reason=proposal['reason']))
    db.flush();return record
