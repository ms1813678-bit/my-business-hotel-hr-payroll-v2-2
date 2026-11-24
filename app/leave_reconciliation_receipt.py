"""Immutable operator assertion receipts, not source verification or approval."""
from dataclasses import asdict
from datetime import datetime,UTC
from hashlib import sha256
import json,re
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_transition_draft import canonical,draft_transition,LedgerDecision,OpeningReleaseDecision
from app.models import LeaveReconciliationReviewReceipt,AuditLog


def receipt_hash(row):
    return sha256(canonical(dict(id=row.id,comparison_id=row.comparison_id,employee_id=row.employee_id,
        actor_id=row.actor_id,session_id=row.session_id,session_version=row.session_version,restore_epoch=row.restore_epoch,
        source_digest=row.source_digest,decision_digest=row.decision_digest,assertions_json=row.assertions_json,
        reason=row.reason,created_at=row.created_at.astimezone(UTC).isoformat())).encode()).hexdigest()


@bounded_workbook_operation
def prepare_assertion_receipt(db,*,operation_id,comparison_id,employee_id,reviewed_source_digest,
        reviewed_decision_digest,ledger_decisions,opening_decisions,reason,human_confirmed,**selection):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    if (not isinstance(operation_id,str) or re.fullmatch('[0-9a-f]{64}',operation_id) is None
            or human_confirmed is not True or not isinstance(reason,str) or not reason.strip() or len(reason)>1000):
        raise ValueError('Explicit reviewed assertion recording required.')
    auth={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    actor=_guard(db,**auth)
    prepared=draft_transition(db,reviewed_reconciliation_digest=reviewed_source_digest,ledger_decisions=ledger_decisions,opening_decisions=opening_decisions,**selection)
    if prepared.decision_digest!=reviewed_decision_digest or {r.employee_id for r in prepared.rows}!={employee_id}:raise ValueError('Reviewed assertion/source scope changed.')
    content=canonical(dict(openings=[asdict(d) for d in opening_decisions],ledger=[asdict(d) for d in ledger_decisions],
        rows=[asdict(r) for r in prepared.rows],release_evaluated_on=selection['release_evaluated_on'],
        selection_ids=selection['selection_ids'],month=selection['month_start']))
    old=db.get(LeaveReconciliationReviewReceipt,operation_id,populate_existing=True)
    if old:
        if (old.comparison_id,old.employee_id,old.actor_id,old.session_id,old.session_version,old.restore_epoch,
            old.source_digest,old.decision_digest,old.assertions_json,old.reason)!=(comparison_id,employee_id,actor.id,
            auth['session_id'],auth['session_version'],auth['expected_epoch'],reviewed_source_digest,reviewed_decision_digest,content,reason.strip()) or receipt_hash(old)!=old.integrity_digest:
            raise ValueError('Recorded assertion identity/integrity changed.')
        return old
    with db.begin_nested():
        row=LeaveReconciliationReviewReceipt(id=operation_id,comparison_id=comparison_id,employee_id=employee_id,
            actor_id=actor.id,session_id=auth['session_id'],session_version=auth['session_version'],restore_epoch=auth['expected_epoch'],
            source_digest=reviewed_source_digest,decision_digest=reviewed_decision_digest,assertions_json=content,
            reason=reason.strip(),created_at=datetime.now(UTC),integrity_digest='0'*64)
        row.integrity_digest=receipt_hash(row)
        db.add(row);db.flush()
        db.add(AuditLog(actor=actor.username,action='leave_reconciliation_assertions_recorded',entity_type='leave_reconciliation_review_receipt',
            entity_id=row.id,new_value=canonical(dict(source_digest=row.source_digest,decision_digest=row.decision_digest,
                integrity_digest=row.integrity_digest,assertions_only=True,verified=False,approved=False,posted=False)),reason=reason.strip()))
        db.flush()
    return row
