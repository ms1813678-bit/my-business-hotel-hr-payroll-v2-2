"""Exact HR reconciliation approval and linked corrections, never posting.

Approval records an accountable human decision, not independent source truth.
Caller owns commit/rollback. Every public operation checks dirty state before
any query, then takes the exclusive restore gate before source/table locks.
"""
from datetime import UTC, date, datetime
from hashlib import sha256
import json
import re

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_reconciliation_receipt import receipt_hash
from app.leave_transition_draft import canonical, draft_transition, LedgerDecision, OpeningReleaseDecision
from app.models import (LeaveReconciliationApproval, LeaveReconciliationReviewReceipt,
                        AuditLog, LeaveMonthClose, PayrollRun)


def _hash(value):
    return sha256(canonical(value).encode()).hexdigest()


def approval_hash(row):
    return _hash({key: (getattr(row,key).astimezone(UTC).isoformat() if key=='created_at' else getattr(row,key))
                  for key in ('id','receipt_id','predecessor_id','scope_key','employee_id','actor_id',
                              'session_id','session_version','restore_epoch','source_digest','decision_digest',
                              'comparison_digest','request_digest','reason','created_at')})


def _clean(db):
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')


def _key(value):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None:
        raise ValueError('Exact reviewed identity required.')
    return value


def _receipt(db,receipt_id,employee_id,epoch):
    row=db.get(LeaveReconciliationReviewReceipt,_key(receipt_id),populate_existing=True)
    if (row is None or row.employee_id!=employee_id or row.restore_epoch!=epoch
            or receipt_hash(row)!=row.integrity_digest):
        raise ValueError('Current intact assertion receipt required.')
    return row


def _current_draft(db,receipt,selection):
    saved=json.loads(receipt.assertions_json)
    from app.leave_reconciliation_source import reconciliation_source
    auth={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    authoritative,_,raw=reconciliation_source(db,receipt.comparison_id,receipt.employee_id,auth)
    if any(selection[k]!=v for k,v in authoritative.items()):
        raise ValueError('Current authoritative receipt source changed.')
    if (saved['month']!=selection['month_start'].isoformat()
            or saved['release_evaluated_on']!=selection['release_evaluated_on'].isoformat()
            or saved['selection_ids']!=list(selection['selection_ids'])):
        raise ValueError('Exact receipt selection required.')
    prepared=draft_transition(db,reviewed_reconciliation_digest=receipt.source_digest,
        ledger_decisions=tuple(LedgerDecision(**d) for d in saved['ledger']),
        opening_decisions=tuple(OpeningReleaseDecision(**d) for d in saved['openings']),**selection)
    if (prepared.decision_digest!=receipt.decision_digest
            or {r.employee_id for r in prepared.rows}!={receipt.employee_id}
            or any(r.unresolved or r.adjustment_delta is None for r in prepared.rows)):
        raise ValueError('Unresolved or changed evidence refuses approval.')
    return prepared,raw.source_digest


def _latest(db,scope):
    rows=db.scalars(select(LeaveReconciliationApproval).where(LeaveReconciliationApproval.scope_key==scope)
                    .execution_options(populate_existing=True)).all()
    if any(approval_hash(r)!=r.integrity_digest for r in rows):
        raise ValueError('Approval integrity mismatch.')
    predecessors={r.predecessor_id for r in rows if r.predecessor_id}
    leaves=[r for r in rows if r.id not in predecessors]
    if len(leaves)>1:
        raise ValueError('Approval chain integrity mismatch.')
    return leaves[0] if leaves else None


def _scope(employee_id,epoch):
    return _hash(dict(employee_id=employee_id,epoch=epoch))


def _barrier(db,month):
    from calendar import monthrange
    end=date(month.year,month.month,monthrange(month.year,month.month)[1])
    if db.scalar(select(LeaveMonthClose).where(LeaveMonthClose.year==month.year,LeaveMonthClose.month==month.month)):
        raise ValueError('Closed leave period refuses approval.')
    if db.scalar(select(PayrollRun.id).where(PayrollRun.period_start<=end,PayrollRun.period_end>=month,
                                          PayrollRun.status.in_(('approved','closed')))):
        raise ValueError('Approved/closed payroll refuses approval.')


def review_approval(db,*,receipt_id,employee_id,**selection):
    """Read current applicability and proposed predecessor, without writes."""
    _clean(db)
    return _review(db,receipt_id=receipt_id,employee_id=employee_id,**selection)


@bounded_workbook_operation
def _review(db,*,receipt_id,employee_id,**selection):
    _guard(db,**{k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')})
    receipt=_receipt(db,receipt_id,employee_id,selection['expected_epoch'])
    prepared,evidence=_current_draft(db,receipt,selection)
    _barrier(db,selection['month_start'])
    latest=_latest(db,_scope(employee_id,selection['expected_epoch']))
    existing=latest if latest and latest.receipt_id==receipt_id and latest.comparison_digest==evidence else None
    return receipt,prepared,existing,latest.id if latest and not existing else None


def approve_reconciliation(db,*,operation_id,receipt_id,employee_id,reviewed_receipt_digest,
                           predecessor_id,reason,human_confirmed,reviewed_comparison_digest,**selection):
    _clean(db)
    return _approve(db,operation_id=operation_id,receipt_id=receipt_id,employee_id=employee_id,
        reviewed_receipt_digest=reviewed_receipt_digest,predecessor_id=predecessor_id,
        reason=reason,human_confirmed=human_confirmed,reviewed_comparison_digest=reviewed_comparison_digest,**selection)


@bounded_workbook_operation
def _approve(db,*,operation_id,receipt_id,employee_id,reviewed_receipt_digest,
             predecessor_id,reason,human_confirmed,reviewed_comparison_digest,**selection):
    _key(operation_id);_key(receipt_id);_key(reviewed_receipt_digest);_key(reviewed_comparison_digest)
    if predecessor_id is not None:_key(predecessor_id)
    if (type(employee_id) is not int or employee_id<1 or human_confirmed is not True
            or not isinstance(reason,str) or not reason.strip() or len(reason)>1000):
        raise ValueError('Explicit HR reason and confirmation required.')
    auth={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_guard(db,**auth)
    # Same global order as source reader. Restore's exclusive gate serializes
    # accepted writers, table locks also protect against ordinary legacy writes.
    db.execute(text('LOCK TABLE leave_reconciliation_approvals, leave_balance_transactions, employee_service_periods, leave_register_entries, leave_records, leave_types, leave_month_close, payroll_runs IN SHARE ROW EXCLUSIVE MODE'))
    receipt=_receipt(db,receipt_id,employee_id,selection['expected_epoch'])
    if receipt.integrity_digest!=reviewed_receipt_digest:
        raise ValueError('Reviewed receipt changed.')
    normalized=dict(selection)
    normalized['current_insurance_years']=sorted((e,m.isoformat(),v) for (e,m),v in selection['current_insurance_years'].items())
    normalized['age_evaluation_dates']=sorted((m.isoformat(),d.isoformat()) for m,d in selection['age_evaluation_dates'].items())
    request=_hash(dict(receipt_id=receipt_id,receipt_digest=reviewed_receipt_digest,employee_id=employee_id,
                       predecessor_id=predecessor_id,comparison_digest=reviewed_comparison_digest,selection=normalized,reason=reason.strip()))
    old=db.get(LeaveReconciliationApproval,operation_id,populate_existing=True)
    if old:
        if old.request_digest!=request or approval_hash(old)!=old.integrity_digest:
            raise ValueError('Approval operation request changed.')
        # A retry returns historical receipt, not a claim of current applicability.
        return old
    scope=_scope(employee_id,selection['expected_epoch'])
    latest=_latest(db,scope)
    if (latest.id if latest else None)!=predecessor_id:
        raise ValueError('Latest approval changed; review explicit correction.')
    if latest and latest.receipt_id==receipt_id:
        raise ValueError('Receipt already approved.')
    _barrier(db,selection['month_start'])
    _,evidence=_current_draft(db,receipt,selection)
    if evidence!=reviewed_comparison_digest:
        raise ValueError("Reviewed comparison changed.")
    with db.begin_nested():
        row=LeaveReconciliationApproval(id=operation_id,receipt_id=receipt_id,predecessor_id=predecessor_id,
            scope_key=scope,employee_id=employee_id,actor_id=actor.id,session_id=auth['session_id'],
            session_version=auth['session_version'],restore_epoch=auth['expected_epoch'],
            source_digest=receipt.source_digest,decision_digest=receipt.decision_digest,comparison_digest=reviewed_comparison_digest,request_digest=request,
            reason=reason.strip(),created_at=datetime.now(UTC),integrity_digest='0'*64)
        row.integrity_digest=approval_hash(row)
        db.add(row);db.flush()
        db.add(AuditLog(actor=actor.username,action='leave_reconciliation_approved',entity_type='leave_reconciliation_approval',
            entity_id=row.id,reason=row.reason,new_value=canonical(dict(receipt_id=receipt.id,
                source_digest=row.source_digest,decision_digest=row.decision_digest,predecessor_id=predecessor_id,
                integrity_digest=row.integrity_digest,approved=True,independently_verified=False,posted=False))))
        db.flush()
    return row
