"""Explicit evidence decisions over a fresh selected-source snapshot.

No UI, implicit approval, persistence, availability or posting. Decisions are
review inputs, not evidence that the supplied assertion is factually correct.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from datetime import date
from hashlib import sha256
import json
import re

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_balance_rules import _bounded_amount, MAX_ARITHMETIC_DIGITS


@dataclass(frozen=True)
class LedgerDecision:
    ledger_id: int
    treatment: str  # included_in_scenario / external_to_scenario / unresolved
    evidence_reference: str


@dataclass(frozen=True)
class OpeningReleaseDecision:
    employee_id: int
    code: str
    released: bool | None
    evidence_reference: str


@dataclass(frozen=True)
class TransitionDraftRow:
    employee_id: int
    code: str
    ledger_ids: tuple
    unresolved: tuple
    reviewed_target: Decimal | None
    adjustment_delta: Decimal | None


@dataclass(frozen=True)
class TransitionDraft:
    rows: tuple
    reconciliation_digest: str
    decision_digest: str
    posting_allowed: bool = False
    establishes_spendable_credit: bool = False


def canonical(value):
    def encode(v):
        if isinstance(v, Decimal):
            return format(v, 'f')
        if type(v) is date:
            return v.isoformat()
        raise TypeError('Unsupported decision value.')
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=encode)


def _reference(value):
    if not isinstance(value, str) or len(value) > 1000 or not value.strip():
        raise ValueError('Explicit bounded evidence reference required.')
    return value


def _exact_sum(values):
    values = tuple(values)
    for v in values:
        _bounded_amount(v)
    precision = max(50, max((v.adjusted() for v in values), default=0)
                    - min((v.as_tuple().exponent for v in values), default=0)
                    + len(str(len(values) + 1)) + 2)
    if precision > MAX_ARITHMETIC_DIGITS:
        raise ValueError('Exact reconciliation arithmetic resource bound exceeded.')
    with localcontext() as context:
        context.prec = precision
        return sum(values, Decimal(0))


@bounded_workbook_operation
def draft_transition(db, *, reviewed_reconciliation_digest, ledger_decisions,
                     opening_decisions, **selection):
    """Exact coverage, fresh source, no automatic acceptance of overlaps.

    'included' means this exact legacy delta is already represented in the
    released scenario. 'external' preserves it in addition to that scenario.
    No row is silently discarded, and unresolved source conflicts cannot be
    overridden by a checkbox. Opening release must be explicitly asserted for
    each category, including zero opening. This is not a release policy engine.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean transaction required.')
    if not isinstance(reviewed_reconciliation_digest, str) or re.fullmatch('[0-9a-f]{64}', reviewed_reconciliation_digest) is None:
        raise ValueError('Exact reviewed reconciliation digest required.')
    if type(ledger_decisions) is not tuple or len(ledger_decisions) > 100000 or any(type(d) is not LedgerDecision for d in ledger_decisions):
        raise ValueError('Bounded explicit ledger decisions required.')
    if type(opening_decisions) is not tuple or not 1 <= len(opening_decisions) <= 900 or any(type(d) is not OpeningReleaseDecision for d in opening_decisions):
        raise ValueError('Bounded explicit opening decisions required.')
    result = reconcile_selected_opening(db, **selection)
    if result.reconciliation_digest != reviewed_reconciliation_digest:
        raise ValueError('Reviewed reconciliation changed.')
    expected_ids = {i for row in result.rows for i in row.ledger_row_ids}
    decisions = {}
    for d in ledger_decisions:
        if type(d.ledger_id) is not int or d.ledger_id < 1 or d.ledger_id in decisions or not isinstance(d.treatment,str) or d.treatment not in {'included_in_scenario', 'external_to_scenario', 'unresolved'}:
            raise ValueError('Unique exact ledger decisions required.')
        _reference(d.evidence_reference)
        decisions[d.ledger_id] = d
    if set(decisions) != expected_ids:
        raise ValueError('Exact ledger decision coverage required.')
    openings = {}
    for d in opening_decisions:
        key = (d.employee_id, d.code)
        if type(d.employee_id) is not int or d.employee_id < 1 or not isinstance(d.code,str) or d.code not in {'A', 'H', 'W'} or key in openings or (d.released is not None and type(d.released) is not bool):
            raise ValueError('Unique exact opening release decisions required.')
        _reference(d.evidence_reference)
        openings[key] = d
    if set(openings) != {(r.employee_id, r.code) for r in result.rows}:
        raise ValueError('Exact opening release coverage required.')
    from sqlalchemy import select
    from app.models import LeaveBalanceTransaction
    ledger = {r.id: r for r in db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.id.in_(expected_ids)).execution_options(populate_existing=True))}
    from app.leave_opening_receipt import IDENTITY_KEYS, verified_receipt_ledger_ids
    bound, unverified = verified_receipt_ledger_ids(db, ledger_rows={i: x.source_operation_key for i, x in ledger.items()},
        selection_ids=selection['selection_ids'], **{k: selection.get(k) for k in IDENTITY_KEYS})
    if any(decisions[i].treatment != 'included_in_scenario' for i in bound):
        # The selected opening is already the scenario carry; its verified apply row can only be 'included'.
        raise ValueError('Verified manual-opening receipt rows can only be included_in_scenario.')
    rows = []
    for r in result.rows:
        unresolved = []
        opening = openings[(r.employee_id, r.code)]
        if opening.released is not True:
            unresolved.append('opening_release_not_attested')
        if 'category_inactive' in r.issues:
            unresolved.append('category_inactive')
        if 'category_not_balance_tracked' in r.issues:
            unresolved.append('category_not_balance_tracked')
        if unverified & set(r.ledger_row_ids):
            unresolved.append('manual_opening_receipt_unverified')
        if r.ledger_total_separate is None:
            unresolved.append('ledger_category_unresolved')
        if any(decisions[i].treatment == 'unresolved' for i in r.ledger_row_ids):
            unresolved.append('ledger_treatment_unresolved')
        for candidate in result.overlap_candidates:
            if ((candidate.employee_id == r.employee_id and candidate.category == r.code)
                    or set(candidate.ledger_ids) & set(r.ledger_row_ids)) and candidate.state != 'explicit_link_candidate_not_reconciled':
                unresolved.append('source_overlap_conflict')
        # Identity-crossed source records taint every selected category, as in
        # the existing explanation layer. Explicit decisions do not repair them.
        if any('identity' in issue for c in result.overlap_candidates for issue in c.issues):
            unresolved.append('source_identity_conflict')
        target = delta = None
        if not unresolved:
            external = [ledger[i].days_delta for i in r.ledger_row_ids if decisions[i].treatment == 'external_to_scenario']
            target = _exact_sum((r.released_scenario_balance, *external))
            delta = _exact_sum((target, r.ledger_total_separate.copy_negate()))
        rows.append(TransitionDraftRow(r.employee_id, r.code, r.ledger_row_ids,
                                      tuple(dict.fromkeys(unresolved)), target, delta))
    from dataclasses import asdict
    payload = dict(reconciliation=result.reconciliation_digest,
                   ledger=sorted((asdict(d) for d in ledger_decisions), key=lambda x:x['ledger_id']),
                   openings=sorted((asdict(d) for d in opening_decisions), key=lambda x:(x['employee_id'],x['code'])),
                   rows=[asdict(r) for r in rows])
    return TransitionDraft(tuple(rows), result.reconciliation_digest,
                           sha256(canonical(payload).encode()).hexdigest())
