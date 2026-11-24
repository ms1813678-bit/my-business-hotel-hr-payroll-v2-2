"""Synthetic-only returned-day factual registration, never approval.

P means explicitly returned presence plus an already consumed same-comparison
attendance receipt. Leave without punches is valid; conflicting punches/unknown
source refuse. No LeaveRecord, balance debit or payroll effects are inferred.
"""
from datetime import date,UTC
from hashlib import sha256
import json
import re
from sqlalchemy import select,text
from app.config import settings
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.shift_day_store import read_saved_shift_days
from app.fingerprint_bounded_parse import parse_operational_fingerprint
from app.leave_transition_draft import canonical
from app.leave_register_review import RegisterEdit,prepare_register_edit
from app.models import (LeaveSyntheticRegisterConsumption,LeaveRegisterEntry,EmployeeServicePeriod,
    AttendanceEvidenceReviewReceipt,AttendanceEvidenceConsumption,AuditLog)


def _hash(v):return sha256(canonical(v).encode()).hexdigest()


def _derive(db,comparison_id,employee_id,identity,expected=None):
    _guard(db,**identity)
    # Match strongest attendance consume locks before its source readers.
    db.execute(text('LOCK TABLE attendance_daily_records,attendance_months,leave_month_close,payroll_runs,attendance_evidence_occurrence_claims,employee_service_periods,leave_register_entries,leave_synthetic_register_consumptions IN SHARE ROW EXCLUSIVE MODE'))
    source=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**identity)
    from app.models import Employee,PayrollRun
    employee=db.get(Employee,employee_id,populate_existing=True)
    from app.models import DepartmentFingerprintPreparation
    saved_comparison=db.get(DepartmentFingerprintPreparation,comparison_id,populate_existing=True)
    aliases={a:i for a,_normalized,i in json.loads(saved_comparison.review_json)['alias_bindings']}
    raw=parse_operational_fingerprint(source.fingerprint_bytes,filename=source.filename_hint,verified_aliases=aliases)
    if any(i.get('reason')!='duplicate_punch' for i in raw.issues) or any(v['employee_id'] is None for v in raw.occurrences):
        raise ValueError('Unresolved original fingerprint evidence blocks registration.')
    days={date.fromisoformat(r['date']):r for r in source.days}
    first=min(days).replace(day=1)
    from calendar import monthrange
    last=date(first.year,first.month,monthrange(first.year,first.month)[1])
    if db.scalar(select(PayrollRun.id).where(PayrollRun.period_start<=last,PayrollRun.period_end>=first,PayrollRun.status.in_(('approved','closed')))):
        raise ValueError('Approved/closed payroll blocks registration.')
    periods=db.scalars(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee_id).execution_options(populate_existing=True)).all()
    employed=[]
    from datetime import timedelta
    for n in range(last.day):
        d=first+timedelta(days=n)
        coverage=sum(p.start_date<=d and (p.end_date is None or p.end_date>=d) for p in periods)
        if coverage>1:raise ValueError('Overlapping service requires review.')
        if coverage:employed.append(d)
    if not employed:raise ValueError('Recorded service required.')
    proposals=[];attendance=[];register_snapshots=[]
    for d in employed:
        saved=days.get(d)
        if saved is None or saved['code'] not in {'P','A','H','W','S','U'}:
            raise ValueError('Every employed day requires explicit returned code.')
        if saved['code']=='P':
            receipts=db.scalars(select(AttendanceEvidenceReviewReceipt).join(AttendanceEvidenceConsumption,AttendanceEvidenceConsumption.receipt_id==AttendanceEvidenceReviewReceipt.id).where(
                AttendanceEvidenceReviewReceipt.comparison_id==comparison_id,AttendanceEvidenceReviewReceipt.employee_id==employee_id,
                AttendanceEvidenceReviewReceipt.work_date==d)).all()
            if len(receipts)!=1:raise ValueError('P requires one already consumed same-source attendance receipt.')
            from app.attendance_evidence_review import consume_evidence_review,_record_digest
            record=consume_evidence_review(db,receipt_id=receipts[0].id,confirmed_request_digest=receipts[0].request_digest,**identity)
            attendance.append((d.isoformat(),receipts[0].id,record.id,_record_digest(record)))
        elif any(v['employee_id']==employee_id and v['punch_at'].date()==d for v in raw.occurrences):
            raise ValueError('Leave/absence with calendar punches requires separate review.')
        code='' if saved['code']=='P' else saved['code']
        edit=RegisterEdit(employee_id,d,code,None,None,'synthetic-return',first,last)
        review=prepare_register_edit(db,edit)
        if review.issues:raise ValueError('Register source blocked: '+','.join(review.issues))
        existing=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,LeaveRegisterEntry.work_date==d).execution_options(populate_existing=True))
        expected_id=(expected or {}).get(d.isoformat())
        if existing is not None:
            if existing.id!=expected_id or existing.code!=code or existing.source!='synthetic-return' or existing.notes is not None or existing.certificate_attached is not None or existing.leave_record_id is not None:
                raise ValueError('Existing register day is not overwritten or adopted.')
        elif expected_id is not None:raise ValueError('Registered result disappeared.')
        if existing is not None:
            register_snapshots.append((existing.id,existing.revision,existing.recorded_by,existing.recorded_at.astimezone(UTC).isoformat(),existing.code,existing.notes,existing.certificate_attached,existing.leave_record_id))
        proposals.append(dict(employee_id=employee_id,date=d.isoformat(),code=code,sheet=saved['sheet'],cell=saved['cell']))
    from app.models import DepartmentWorkbookPreparation,DepartmentWorkbookExport,DepartmentPreparationContract
    from app.department_contract import decode_selection
    from app.department_preparation_workflow import _confirmed_department_projection
    returned=db.get(DepartmentWorkbookPreparation,saved_comparison.return_id,populate_existing=True)
    exported=db.get(DepartmentWorkbookExport,returned.export_id,populate_existing=True)
    contract=db.get(DepartmentPreparationContract,exported.id,populate_existing=True)
    if contract is None or sha256(contract.selection_json.encode()).hexdigest()!=contract.selection_sha256:
        raise ValueError('Current confirmed source contract required.')
    projection,_department=_confirmed_department_projection(db,**decode_selection(contract.selection_json),**identity)
    if expected is None and projection.source_digest!=exported.source_digest:
        raise ValueError('Current export source changed before registration.')
    # Include live projection in the pre-review digest. On replay the target
    # register has intentionally changed: caller verifies separately stored
    # post-source digest instead of pretending the pre-source stayed identical.
    digest=_hash(dict(register_snapshots=register_snapshots,current_projection=projection.source_digest,source=source.source_digest,days=proposals,attendance=attendance,
        service=sorted((p.id,p.start_date.isoformat(),p.end_date.isoformat() if p.end_date else None) for p in periods)))
    return proposals,digest


@bounded_workbook_operation
def preview_synthetic_register(db,*,comparison_id,employee_id,**identity):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    return _derive(db,comparison_id,employee_id,identity)


@bounded_workbook_operation
def consume_synthetic_register(db,*,operation_id,comparison_id,employee_id,reviewed_digest,human_confirmed,reason,**identity):
    if (not settings.leave_synthetic_register_enabled or settings.app_env.strip().lower() not in {'test','synthetic'}
            or not (db.get_bind().url.database or '').startswith('synthetic_cycle_')):
        raise ValueError('Local synthetic register bridge disabled.')
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or human_confirmed is not True or not isinstance(reason,str) or not reason.strip() or len(reason)>1000):
        raise ValueError('Explicit operation, human confirmation and reason required.')
    actor=_guard(db,**identity)
    db.execute(text('LOCK TABLE leave_synthetic_register_consumptions IN SHARE ROW EXCLUSIVE MODE'))
    old=db.get(LeaveSyntheticRegisterConsumption,operation_id,populate_existing=True)
    expected=None
    if old:
        if (old.comparison_id,old.employee_id,old.actor_id,old.session_id,old.session_version,old.restore_epoch)!=(comparison_id,employee_id,actor.id,identity['session_id'],identity['session_version'],identity['expected_epoch']):
            raise ValueError('Stored operation scope/authority changed.')
        stored=json.loads(old.result_json)
        if stored['reason']!=reason.strip():raise ValueError('Stored operation reason changed.')
        expected=stored['ids']
    elif db.scalar(select(LeaveSyntheticRegisterConsumption.id).where(LeaveSyntheticRegisterConsumption.comparison_id==comparison_id,LeaveSyntheticRegisterConsumption.employee_id==employee_id)):
        raise ValueError('Comparison employee already consumed.')
    proposals,digest=_derive(db,comparison_id,employee_id,identity,expected)
    if old:
        if reviewed_digest!=old.source_digest or digest!=stored['post_digest']:
            raise ValueError('Reviewed source/result changed.')
        return old
    if digest!=reviewed_digest:raise ValueError('Reviewed source/result changed.')
    with db.begin_nested():
        ids={}
        for p in proposals:
            row=LeaveRegisterEntry(employee_id=employee_id,work_date=date.fromisoformat(p['date']),code=p['code'],
                certificate_attached=None,notes=None,source='synthetic-return',recorded_by=actor.username,revision=1)
            db.add(row);db.flush();ids[p['date']]=row.id
        # Fresh post-source is recomputed only after all register writes.
        _post_proposals,post_digest=_derive(db,comparison_id,employee_id,identity,ids)
        row=LeaveSyntheticRegisterConsumption(id=operation_id,comparison_id=comparison_id,employee_id=employee_id,
            actor_id=actor.id,session_id=identity['session_id'],session_version=identity['session_version'],restore_epoch=identity['expected_epoch'],
            source_digest=digest,result_json=canonical(dict(ids=ids,post_digest=post_digest,reason=reason.strip())))
        db.add(row)
        db.add(AuditLog(actor=actor.username,action='synthetic_return_registered',entity_type='leave_synthetic_register_consumption',
            entity_id=row.id,new_value=canonical(dict(days=proposals,ids=ids,source_digest=digest)),reason=reason.strip()))
        db.flush()
    return row
