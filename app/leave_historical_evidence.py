"""Authenticated HR preparation of exact historical opening/carry source evidence.

No route, parser, balance application, grant, accepted review, or payroll effect.
A proposal requires later human review and a separately authorized apply service.
Source bytes must come from the actual uploaded source; their hash is not proof
of authorship, correctness or approval. Resolved IDs are rechecked, not matched
by name here. A source cell is provenance, never authority to change a balance.
"""
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.models import AuditLog, Employee, LeaveHistoricalEvidencePreparation
from app.restore_lock import read_restore_epoch


@dataclass(frozen=True)
class HistoricalLeaveFact:
    employee_id: int
    code: str
    kind: str  # opening, carry_grant, debt_carry, adjustment, all still proposals.
    days: Decimal
    effective_from: date
    effective_through: date
    source_sheet: str
    source_cell: str
    reason: str


def _fact_json(fact):
    if not isinstance(fact, HistoricalLeaveFact):
        raise TypeError('Explicit historical source fact required.')
    if type(fact.employee_id) is not int or fact.employee_id < 1:
        raise ValueError('Resolved employee identity required.')
    if type(fact.code) is not str or fact.code not in ('A', 'H', 'W'):
        raise ValueError('Explicit leave category required.')
    if type(fact.kind) is not str or fact.kind not in ('opening', 'carry_grant', 'debt_carry', 'adjustment'):
        raise ValueError('Explicit historical evidence kind required.')
    if not isinstance(fact.days, Decimal) or not fact.days.is_finite():
        raise ValueError('Exact finite Decimal days required.')
    tup = fact.days.as_tuple()
    if len(tup.digits) > 100 or abs(tup.exponent) > 100:
        raise ValueError('Historical source decimal exceeds bounded evidence envelope.')
    if ((fact.kind == 'opening' and fact.days < 0)
            or (fact.kind == 'carry_grant' and fact.days <= 0)
            or (fact.kind == 'debt_carry' and fact.days >= 0)
            or (fact.kind == 'adjustment' and fact.days == 0)):
        raise ValueError('Evidence sign does not match opening/grant/debt/adjustment kind.')
    if any(type(d) is not date for d in (fact.effective_from, fact.effective_through)) or fact.effective_from > fact.effective_through:
        raise ValueError('Explicit effective window required.')
    for value in (fact.source_sheet, fact.source_cell, fact.reason):
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            raise ValueError('Bounded source coordinates and review reason required.')
    return {'employee_id': fact.employee_id, 'code': fact.code, 'kind': fact.kind,
            'days': format(fact.days, 'f'), 'effective_from': fact.effective_from.isoformat(),
            'effective_through': fact.effective_through.isoformat(),
            'source_sheet': fact.source_sheet, 'source_cell': fact.source_cell, 'reason': fact.reason}


@bounded_workbook_operation
def prepare_historical_evidence(db, *, source_bytes, source_label, facts,
                                actor_id, session_id, session_version, expected_epoch):
    """Persist proposed evidence atomically, never apply a balance.

    No auto consumption by workbook projection. Exact retries share proposal;
    changed source/mapping/value/window/kind remains a distinct proposal, not
    replacement. The table lock serializes deduplication until caller commit.
    Persist checksum is defense in depth, not signing. Caller owns transaction.
    """
    actor = _guard(db, actor_id=actor_id, session_id=session_id, session_version=session_version,
           expected_epoch=expected_epoch)
    if db.get_bind().dialect.name != 'postgresql':
        raise RuntimeError('Historical source preparation requires PostgreSQL.')
    if not isinstance(source_bytes, bytes) or not 1 <= len(source_bytes) <= 10_000_000:
        raise ValueError('Actual bounded historical source bytes required.')
    if not isinstance(source_label, str) or not source_label.strip() or len(source_label) > 255:
        raise ValueError('Bounded source label required.')
    if type(facts) is not tuple or not 1 <= len(facts) <= 900:
        raise ValueError('Bounded explicit historical facts required.')
    entries = [_fact_json(fact) for fact in facts]
    # No duplicate cell or ambiguous overlapping facts of a kind in one source.
    if len({(e['source_sheet'], e['source_cell']) for e in entries}) != len(entries):
        raise ValueError('Duplicate source coordinates require review.')
    for i, fact in enumerate(facts):
        for other in facts[:i]:
            if ((fact.employee_id, fact.code, fact.kind) == (other.employee_id, other.code, other.kind)
                    and fact.effective_from <= other.effective_through and other.effective_from <= fact.effective_through):
                raise ValueError('Overlapping historical evidence requires review.')
    db.execute(text('LOCK TABLE employees IN SHARE MODE'))
    ids = {fact.employee_id for fact in facts}
    actual = set(db.scalars(select(Employee.id).where(Employee.id.in_(ids))))
    if actual != ids:
        raise ValueError('Resolved employee identity no longer exists.')
    digest = sha256(source_bytes).hexdigest()
    proposal = json.dumps({'version': 1, 'source_sha256': digest, 'source_label': source_label,
        'entries': sorted(entries, key=lambda e: (e['employee_id'], e['code'], e['kind'], e['effective_from'], e['source_sheet'], e['source_cell'])),
        'requires_human_review': True, 'applied': False}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    proposal_hash = sha256(proposal.encode()).hexdigest()
    db.execute(text('LOCK TABLE leave_historical_evidence_preparations IN SHARE ROW EXCLUSIVE MODE'))
    row = db.scalar(select(LeaveHistoricalEvidencePreparation).where(
        LeaveHistoricalEvidencePreparation.proposal_sha256 == proposal_hash))
    if row is not None:
        if row.restore_epoch != read_restore_epoch(db) or row.proposal_json != proposal:
            raise ValueError('Historical proposal integrity or restore epoch mismatch.')
        return row
    row = LeaveHistoricalEvidencePreparation(id=secrets.token_hex(32), actor_id=actor_id,
        source_sha256=digest, proposal_json=proposal, proposal_sha256=proposal_hash,
        restore_epoch=read_restore_epoch(db), created_at=datetime.now(UTC))
    db.add(row); db.flush()
    db.add(AuditLog(actor=actor.username, action='leave_historical_evidence_prepared',
        entity_type='leave_historical_evidence_preparation', entity_id=row.id,
        old_value=None, new_value=json.dumps({'proposal_sha256': proposal_hash,
            'source_sha256': digest, 'fact_count': len(entries), 'applied': False}),
        reason='Historical source proposal only, human review and separate authorized apply required'))
    db.flush()
    return row
