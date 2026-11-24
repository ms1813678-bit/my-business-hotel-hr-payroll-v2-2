"""Manual HR opening preparation and review, never ledger acceptance.

Reuses historical source proposal storage with an explicitly MANUAL source,
not fabricated spreadsheet provenance. No acceptance or grants are registered.
"""
import json
import re
from calendar import monthrange
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from app.leave_historical_evidence import HistoricalLeaveFact, prepare_historical_evidence


def prepare_manual_opening(db, *, employee_id, code, days, effective_start, reason,
                           actor_id, session_id, session_version, expected_epoch):
    """Save exact entered values for human review, not usable balance.

    December31 below is the boundary of this calendar-year projection source,
    NOT an expiry or a carry-window end. The UI must label that distinction.
    Midyear entries can be prepared. Review projection clips to this basis;
    workbook handoff additionally checks its target-month usage compatibility.
    """
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:
        raise ValueError('Bounded manual opening reason required.')
    if type(effective_start) is not date:
        raise TypeError('Explicit opening effective start required.')
    if not isinstance(days, Decimal) or not days.is_finite() or days < 0:
        raise ValueError('Manual opening must be exact finite nonnegative days; debt carry is separate.')
    from app.department_workbook import _decimal
    _decimal(days)  # Existing workbook numeric envelope, no new business cap.
    if days == 0:
        days = Decimal(0)
    else:
        text = format(days, 'f')
        days = Decimal(text.rstrip('0').rstrip('.') if '.' in text else text)
    source = json.dumps({'source_kind': 'manual_hr_opening', 'employee_id': employee_id,
        'code': code, 'days': format(days, 'f'), 'effective_start': effective_start.isoformat(),
        'reason': reason, 'projection_year': effective_start.year}, sort_keys=True, separators=(',', ':')).encode()
    fact = HistoricalLeaveFact(employee_id, code, 'opening', days, effective_start,
        date(effective_start.year, 12, 31), 'manual_hr_opening', code, reason)
    return prepare_historical_evidence(db, source_bytes=source,
        source_label='Manual HR opening, not an uploaded file', facts=(fact,), actor_id=actor_id,
        session_id=session_id, session_version=session_version, expected_epoch=expected_epoch)


def _review_manual_opening(row):
    """Read-only review shape. A review is never approval or apply authority."""
    if sha256(row.proposal_json.encode()).hexdigest() != row.proposal_sha256:
        raise ValueError('Manual proposal integrity mismatch.')
    proposal = json.loads(row.proposal_json)
    if not isinstance(proposal, dict):
        raise ValueError('Manual proposal object required.')
    if (proposal.get('requires_human_review') is not True or proposal.get('applied') is not False
            or proposal.get('status', 'pending') != 'pending'
            or proposal.get('superseded', False) is not False):
        raise ValueError('Pending unapplied manual proposal required.')
    entries = proposal.get('entries', [])
    if not isinstance(entries,list) or len(entries) != 1 or not isinstance(entries[0],dict):
        raise ValueError('Single manual proposal entry required.')
    if entries[0]['source_sheet'] != 'manual_hr_opening' or entries[0]['kind'] != 'opening':
        raise ValueError('Explicit single manual opening proposal required.')
    entry = entries[0]
    if (type(entry['employee_id']) is not int or entry['employee_id']<1
            or entry['code'] not in ('A','W','H')
            or not isinstance(entry['days'],str) or len(entry['days'])>200
            or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?',entry['days']) is None
            or not isinstance(entry['reason'],str) or not entry['reason'].strip() or len(entry['reason'])>1000
            or not isinstance(entry['effective_from'],str)
            or date.fromisoformat(entry['effective_from']).isoformat()!=entry['effective_from']):
        raise ValueError('Malformed manual proposal entry fields.')
    amount = Decimal(entry['days'])
    from app.department_workbook import _decimal
    _decimal(amount)
    if amount < 0:
        raise ValueError('Manual opening cannot be negative.')
    return {'proposal_id': row.id, 'proposal_sha256': row.proposal_sha256, 'employee_id': entry['employee_id'], 'code': entry['code'], 'days': entry['days'],
        'effective_start': entry['effective_from'], 'reason': entry['reason'],
        'projection_year': date.fromisoformat(entry['effective_from']).year,
        'requires_human_review': True, 'applied': False,
        'current_ledger_balance_is_not_comparable_opening': True}


def review_manual_opening(row):
    """Normalize malformed stored-source failures into a closed review refusal."""
    try:
        return _review_manual_opening(row)
    except (KeyError, TypeError, AttributeError, InvalidOperation, OverflowError) as exc:
        raise ValueError('Malformed manual proposal source.') from exc


def manual_opening_projection_inputs(*, reviews, facts):
    """Synthetic/review preview only, never accepted balance source.

    Complete A/H/W coverage and year/hire-start compatibility required. A
    midyear opening after employment began must not add full YTD accrual again.
    No missing category is zero. No latest-proposal-wins selection.
    """
    source = json.loads(facts.canonical_json)
    first = date.fromisoformat(source['window_start'])
    target = date.fromisoformat(source['month_start'])
    employees = {e['id']: e for e in source['employees']}
    if not employees:
        raise ValueError('Nonempty employee scope required.')
    result = {}
    for review in reviews:
        i, code = review['employee_id'], review['code']
        if i not in employees or (i, code) in result or code not in ('A', 'H', 'W'):
            raise ValueError('Unambiguous complete manual source scope required.')
        service = [date.fromisoformat(p['start_date']) for p in source['service_periods'] if p['employee_id'] == i]
        if not service:
            raise ValueError('Employment evidence required.')
        basis = date.fromisoformat(review['effective_start'])
        if basis < max(first, min(service)) or basis > date(target.year, target.month, monthrange(target.year, target.month)[1]):
            raise ValueError('Opening basis needs effective-date clipped projection; full YTD addition refused.')
        amount = Decimal(review['days'])
        from app.department_workbook import _decimal
        _decimal(amount)
        if amount < 0:
            raise ValueError('Manual opening cannot be negative.')
        result[i, code] = amount
    if set(result) != {(i, code) for i in employees for code in ('A', 'H', 'W')}:
        raise ValueError('Complete explicit A/H/W opening preview required.')
    starts = {review['effective_start'] for review in reviews}
    if len(starts) != 1:
        raise ValueError('One shared opening basis per preview required; mixed dates need separate previews.')
    return result, date.fromisoformat(next(iter(starts)))


def manual_opening_projection_preview(*, db, reviews, facts, policy, annual_tiers,
                                      planning_target_month=False):
    """Review-only projection, never treat pending manual values as accepted.

    Selected stored proposal references are checked before projection. The
    projection carries its usage basis into both plain and guarded builders.
    """
    from app.department_workbook_projection import project_earned_workbook
    from app.models import LeaveHistoricalEvidencePreparation
    for review in reviews:
        row = db.get(LeaveHistoricalEvidencePreparation, review.get('proposal_id'), populate_existing=True)
        if row is None or review_manual_opening(row) != review:
            raise ValueError('Stored manual proposal review binding required.')
    opening, start = manual_opening_projection_inputs(reviews=reviews, facts=facts)
    return project_earned_workbook(facts=facts, policy=policy, opening=opening,
        annual_tiers=annual_tiers, planning_target_month=planning_target_month,
        opening_effective_start=start)


def prepare_manual_opening_group(db, *, employee_id, balances, effective_start, reason, **identity):
    """Selected option2: all categories succeed or none, caller owns commit.

    Savepoint rolls back partial proposal/audit creation on any invalid field.
    A review screen has no approval/apply endpoint in this preparation stage.
    """
    if type(balances) is not dict or set(balances) != {'A', 'H', 'W'}:
        raise ValueError('Explicit complete three-category opening entry required.')
    with db.begin_nested():
        rows = [prepare_manual_opening(db, employee_id=employee_id, code=code,
            days=balances[code], effective_start=effective_start, reason=reason, **identity)
            for code in ('A', 'W', 'H')]
        return tuple(review_manual_opening(row) for row in rows)


def build_manual_opening_preview_workbook(*, projection, department, month_start):
    """Review-only planning artifact, not acceptance or a real export route.

    Full-month service/day evidence remains editable. Balance usage COUNTIF
    begins at the opening basis if inside target month, never double-debits
    earlier days already represented in opening. Formula+visible basis warning
    are covered by immutable digest and the persisted manifest checksum.
    """
    from app.department_workbook import build_department_workbook
    from app.department_workbook_projection import projection_integrity
    if projection_integrity(projection) != projection.integrity_sha256:
        raise ValueError('Projection content changed since computation.')
    if projection.establishes_spendable_credit:
        raise ValueError('Only planning review projection expected.')
    start = projection.opening_effective_start
    return build_department_workbook(department=department, month_start=month_start,
        employees=projection.employees, balance_usage_from=max(month_start, start),
        pending_manual_source=True)
