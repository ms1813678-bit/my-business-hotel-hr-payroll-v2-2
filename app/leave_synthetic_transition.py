"""Unrouted synthetic-only append-only transaction acceptance harness.

Not an actual-data release mechanism. Explicit fixture assertions required.
Caller owns commit/rollback; nested savepoint protects partial local failure.
One scope may transition once, regardless of request or policy-version changes.
"""
from app.leave_exact_amount import canonical_amount, ledger_amount
from dataclasses import asdict
from datetime import datetime, UTC
from hashlib import sha256
import json
import re

from sqlalchemy import select, text
from app.config import settings
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_transition_draft import canonical, draft_transition
from app.leave_source_reconciliation import reconcile_selected_opening
from app.models import LeaveSyntheticTransitionEntry, LeaveSyntheticTransition, LeaveBalanceTransaction, LeaveType, AuditLog, LeaveMonthClose, PayrollRun


def _hash(value):
    return sha256(canonical(value).encode()).hexdigest()


def _synthetic(db):
    if (not settings.leave_synthetic_transition_enabled
            or settings.app_env.strip().lower() not in {'test', 'synthetic'}
            or not (db.get_bind().url.database or '').startswith('synthetic_leave_')):
        raise ValueError('Local synthetic transition is disabled.')


@bounded_workbook_operation
def apply_synthetic_transition(db, *, operation_id, reviewed_decision_digest,
        reviewed_reconciliation_digest, ledger_decisions, opening_decisions,
        reason, human_confirmed, **selection):
    _synthetic(db)
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id, str) or re.fullmatch('[0-9a-f]{64}', operation_id) is None
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000
            or human_confirmed is not True):
        raise ValueError('Explicit operation, reason and human confirmation required.')
    if not isinstance(reviewed_decision_digest, str) or re.fullmatch('[0-9a-f]{64}', reviewed_decision_digest) is None:
        raise ValueError('Exact reviewed decision digest required.')
    identity = {k: selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor = _guard(db, **identity)
    # Strongest locks before the source reader's SHARE locks; restore gate first.
    db.execute(text('LOCK TABLE leave_balance_transactions, leave_synthetic_transitions, leave_synthetic_transition_entries, leave_month_close, payroll_runs IN SHARE ROW EXCLUSIVE MODE'))
    month = selection['month_start']
    if db.scalar(select(LeaveMonthClose).where(LeaveMonthClose.year==month.year, LeaveMonthClose.month==month.month)):
        raise ValueError('Closed leave period refuses transition.')
    if db.scalar(select(PayrollRun).where(PayrollRun.period_start<=month, PayrollRun.period_end>=month, PayrollRun.status.in_(('approved','closed')))):
        raise ValueError('Approved/closed payroll refuses transition.')
    normalized_selection = dict(selection)
    normalized_selection['current_insurance_years'] = sorted((e,m.isoformat(),v) for (e,m),v in selection['current_insurance_years'].items())
    normalized_selection['age_evaluation_dates'] = sorted((m.isoformat(),d.isoformat()) for m,d in selection['age_evaluation_dates'].items())
    request = _hash(dict(selection=normalized_selection, reviewed_reconciliation_digest=reviewed_reconciliation_digest,
        reviewed_decision_digest=reviewed_decision_digest,
        ledger=[asdict(d) for d in ledger_decisions], openings=[asdict(d) for d in opening_decisions], reason=reason.strip()))
    existing = db.get(LeaveSyntheticTransition, operation_id, populate_existing=True)
    if existing:
        if existing.request_digest != request or existing.actor_id != actor.id or existing.session_id != identity['session_id'] or existing.session_version != identity['session_version'] or existing.restore_epoch != identity['expected_epoch']:
            raise ValueError('Operation request or authority changed.')
        current = reconcile_selected_opening(db, **selection)
        if current.reconciliation_digest != existing.post_reconciliation_digest:
            raise ValueError('Applied source/result changed; no replay.')
        _verify_entries(db, existing)
        return existing
    from app.models import LeaveOpeningSelection
    employee_ids = set(db.scalars(select(LeaveOpeningSelection.employee_id).where(LeaveOpeningSelection.id.in_(selection['selection_ids']))))
    if len(employee_ids) != 1 or len(selection['selection_ids']) != 1:
        raise ValueError('Synthetic baseline transition is one selected employee only.')
    # One baseline per employee/epoch, not per policy/selection/month. Later
    # accruals or corrections require their separately designed workflows.
    scope = _hash(dict(employee_id=next(iter(employee_ids)), epoch=identity['expected_epoch']))
    if db.scalar(select(LeaveSyntheticTransition.id).where(LeaveSyntheticTransition.scope_key==scope)):
        raise ValueError('Source scope already transitioned; separate reviewed correction required.')
    draft = draft_transition(db, reviewed_reconciliation_digest=reviewed_reconciliation_digest,
                             ledger_decisions=ledger_decisions, opening_decisions=opening_decisions, **selection)
    if draft.decision_digest != reviewed_decision_digest:
        raise ValueError('Reviewed decisions changed.')
    if any(r.unresolved or r.adjustment_delta is None for r in draft.rows):
        raise ValueError('Unresolved source/decision blocks transition.')
    mapping = {'A':'annual','H':'official_holiday','W':'weekly_rest'}
    result = []
    with db.begin_nested():
        for r in draft.rows:
            kind = db.scalars(select(LeaveType).where(LeaveType.code==mapping[r.code])).all()
            if len(kind)!=1 or not kind[0].tracks_balance or not kind[0].active:
                raise ValueError('Exact balance-tracked category required.')
            row = None
            from decimal import Decimal
            if not isinstance(r.adjustment_delta, Decimal) or not isinstance(r.reviewed_target, Decimal):
                raise ValueError('Synthetic transition does not post recurring rational amounts.')
            if r.adjustment_delta:
                row = LeaveBalanceTransaction(employee_id=r.employee_id,leave_type_id=kind[0].id,
                    transaction_type='adjustment',days_delta=r.adjustment_delta,reason=reason.strip(),created_by=actor.username,
                    # Aggregate baseline delta spans opening/history and is NOT
                    # a monthly earning/year release. Separate immutable links
                    # record provenance without fabricating accrual metadata.
                    source_operation_key=None)
                db.add(row); db.flush()
            result.append(dict(employee_id=r.employee_id,code=r.code,target=format(r.reviewed_target,'f'),
                               delta=format(r.adjustment_delta,'f'),ledger_id=row.id if row else None))
        # Re-read includes exact new signed deltas. This is a source-integrity
        # replay binding, never a claim that existing availability UI changed.
        after = reconcile_selected_opening(db, **selection)
        operation = LeaveSyntheticTransition(id=operation_id,scope_key=scope,actor_id=actor.id,
            session_id=identity['session_id'],session_version=identity['session_version'],restore_epoch=identity['expected_epoch'],
            request_digest=request,result_json=canonical(result),post_reconciliation_digest=after.reconciliation_digest,
            created_at=datetime.now(UTC))
        db.add(operation)
        db.flush()
        for item in result:
            ledger = db.get(LeaveBalanceTransaction, item['ledger_id']) if item['ledger_id'] else None
            db.add(LeaveSyntheticTransitionEntry(operation_id=operation.id, employee_id=item['employee_id'],
                code=item['code'], ledger_id=item['ledger_id'], ledger_digest=_ledger_digest(ledger) if ledger else None))
        db.add(AuditLog(actor=actor.username,action='synthetic_leave_transition',entity_type='leave_synthetic_transition',
            entity_id=operation.id,old_value=None,new_value=canonical(result),reason=reason.strip()))
        db.flush()
    return operation


def _ledger_digest(row):
    return _hash((row.id,row.employee_id,row.leave_type_id,row.leave_record_id,row.transaction_type,
                  canonical_amount(ledger_amount(row)),row.reason,row.created_by,row.source_operation_key,row.source_digest,row.earning_year,row.released_on))


def _verify_entries(db, operation):
    entries = db.scalars(select(LeaveSyntheticTransitionEntry).where(LeaveSyntheticTransitionEntry.operation_id==operation.id)).all()
    result = json.loads(operation.result_json)
    if len(entries)!=len(result) or len(entries)!=3:
        raise ValueError('Applied operation linkage changed.')
    bykey={(r['employee_id'],r['code']):r for r in result}
    for entry in entries:
        item=bykey.get((entry.employee_id,entry.code))
        if item is None or item['ledger_id']!=entry.ledger_id:
            raise ValueError('Applied operation linkage changed.')
        if entry.ledger_id is not None:
            row=db.get(LeaveBalanceTransaction,entry.ledger_id,populate_existing=True)
            if row is None or _ledger_digest(row)!=entry.ledger_digest:
                raise ValueError('Applied ledger changed.')
