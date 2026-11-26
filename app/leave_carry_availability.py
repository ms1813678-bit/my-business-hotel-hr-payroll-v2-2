"""Source-bound READ-ONLY carry availability for one recorded allocation.

Reads exactly one immutable LeaveCarryAllocation receipt and answers, for an
explicit employee/type and an explicit as-of work_date: what exact source
amount the receipt granted, inside which exact inclusive availability window,
and what positive grant ceiling that proves at work_date. Nothing is posted:
no ledger write, no new credit, no cash, no automatic expiry debit, and
posting_allowed is always False.

Verification is against the ALREADY-RECORDED proof, never a re-derivation:
this module deliberately does NOT call review_completed_year_source or the
allocation writer's _derive. Re-running prior-year earning calculations after
a later valid debit changes the live source and would falsely label every
receipt stale. Instead it verifies, inside the caller's transaction:

- current reader authority via the existing guard inside _locks (the same
  audit-first locking entry the allocation writer uses; _locks locks
  audit_logs first), then a SHARE lock on the allocation table;
- the receipt's request/result digests, its exact request/result linkage
  (request['plan'] is the result, request['reviewed_digest'] is the result
  digest), and the original grant actor/session/version/restore-epoch stored
  in the request -- a consistent recomputed-digest forgery of the receipt
  alone still fails the separate audit binding and the source binding below;
- exactly one allocation audit row bound to the original actor username,
  the receipt source digest, the exact result_json text and the reason;
- the recorded source proof embedded in the result: the completed-year
  payload digest, the recorded ledger rows (cross-checked field-by-field
  against the current ledger rows they name -- later NEW debit rows do not
  disturb them), the recorded monthly-receipt bindings re-verified with the
  existing _verify_receipt, and any year-attribution receipts re-verified
  with the existing verify_year_attribution. The carried amount is
  recomputed exactly from those recorded rows/attributions only.

The reader's current authorized session MAY differ from the original grant
session: the guard authenticates the reader as of now, while the original
grant proof stays bound to its original session evidence in the output and
is never rewritten or compared against the reader's session.

Honest limits, returned explicitly rather than papered over:
- No supplied source records which later debits consumed THIS grant, and an
  all-time balance alone is not proof of which grant was spent. The result
  therefore carries the original grant ceiling, marks
  is_remaining_spendable=False with remaining_spendable=None, and lists
  consumption provenance as unresolved. It never labels the original grant
  amount as unspent.
- Source-year completeness is inherited from the completed-year receipt's
  recorded covered-month list, bound here to its verified monthly receipts;
  it is not independently re-derived.
- The allocation's grant-time reviewed_on is not persisted in the receipt,
  so the grant-time ordering reviewed_on <= available_from (enforced by the
  writer when the receipt was recorded) cannot be re-proven from the
  receipt alone; the stored window itself is verified as recorded.

Eligibility: positive carry is eligible only inside the EXACT inclusive
[available_from, available_through] window stored in the receipt -- no fixed
January start and no automatic year-end cutoff is invented, and target_year
is receipt identity, not an extra date restriction. Outside the window the
preserved amount is returned as unavailable; it is never erased and no
expiry debit is posted. Debt carry is not positive availability and a zero
source is not credit. Leave types are addressed by stable ID; any active
balance-tracking type (including custom types) is supported, while inactive
or non-tracking types refuse, as the existing source services require.
Positive annual receipts cannot exist (the writer routes positive annual to
a separate settlement service), so one presented here refuses as forged.
Annual settlement itself is out of scope.
"""
import json
import re
from datetime import date
from fractions import Fraction

from sqlalchemy import select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_exact_amount import canonical_amount, exact_sum, ledger_amount
from app.leave_month_earning import _locks, _verify_receipt, digest
from app.leave_year_attribution_store import verify_year_attribution
from app.models import (
    AdminAccount,
    AuditLog,
    Employee,
    LeaveBalanceTransaction,
    LeaveCarryAllocation,
    LeaveMonthEarning,
    LeaveType,
    LeaveYearAttribution,
)

_RESULT_KEYS = {'employee_id', 'leave_type_id', 'source_year', 'target_year', 'source_digest',
    'source', 'carried_amount', 'ledger_delta', 'cash_amount', 'held_annual',
    'available_from', 'available_through', 'reason', 'operational_spending_allowed'}
_REQUEST_KEYS = {'plan', 'reviewed_digest', 'actor_id', 'actor_username',
    'session_id', 'session_version', 'restore_epoch'}
_COMPLETED_KEYS = {'source', 'selection_id', 'opening_digest', 'opening_apply',
    'covered_months', 'verified_months', 'live_final_source', 'current_tiers', 'reviewed_on'}
_CARRY_KEYS = {'employee_id', 'leave_type_id', 'leave_type_code', 'tracks_balance', 'active',
    'source_year', 'reviewed_on', 'rows', 'monthly_receipts', 'year_attributions',
    'held_annual', 'selected_ids', 'amount'}
_HEX64 = '[0-9a-f]{64}'
UNRESOLVED = ('later_debit_consumption_attribution_not_recorded',
    'source_year_completeness_inherited_from_completed_year_receipt_not_rederived',
    'grant_time_reviewed_on_not_persisted_in_receipt')


def _exact_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f'Carry {label} evidence changed.')
    return value


def _hex64(value, label):
    if not isinstance(value, str) or re.fullmatch(_HEX64, value) is None:
        raise ValueError(f'Carry {label} evidence changed.')
    return value


def _recorded_amount(value, label):
    if not isinstance(value, str):
        raise ValueError(f'Carry {label} evidence changed.')
    try:
        amount = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f'Carry {label} evidence changed.') from exc
    if canonical_amount(amount) != value:
        raise ValueError(f'Carry {label} evidence changed.')
    return amount


def _recorded_date(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f'Carry {label} evidence changed.')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f'Carry {label} evidence changed.') from exc
    if parsed.isoformat() != value:
        raise ValueError(f'Carry {label} evidence changed.')
    return parsed


def evaluate_carry_window(*, carried_amount, available_from, available_through, work_date):
    """Pure window/sign evaluation of an already-verified grant. No DB, no policy.

    Positive grants require the exact stored inclusive window; debt and zero
    grants must carry no window and are never positive availability.
    """
    amount = Fraction(carried_amount)
    if type(work_date) is not date:
        raise ValueError('Explicit work date required.')
    if amount > 0:
        if type(available_from) is not date or type(available_through) is not date or available_from > available_through:
            raise ValueError('Positive carry requires its recorded availability window.')
        within = available_from <= work_date <= available_through
        return dict(kind='positive', within_window=within, eligible=within,
            available_grant_ceiling=amount if within else Fraction(),
            preserved_unavailable=Fraction() if within else amount, debt_amount=Fraction())
    if available_from is not None or available_through is not None:
        raise ValueError('Debt or zero carry has no positive availability grant.')
    if amount < 0:
        return dict(kind='debt', within_window=None, eligible=False,
            available_grant_ceiling=Fraction(), preserved_unavailable=Fraction(), debt_amount=-amount)
    return dict(kind='zero', within_window=None, eligible=False,
        available_grant_ceiling=Fraction(), preserved_unavailable=Fraction(), debt_amount=Fraction())


def verify_allocation_evidence(*, row_id, row_employee_id, row_leave_type_id, row_source_year,
        row_request_digest, row_source_digest, row_result_digest, row_request_json, row_result_json,
        audits, original_actor_username):
    """Pure receipt/request/audit binding check on recorded values. No DB.

    audits is a sequence of (actor, old_value, new_value, reason) tuples for
    the allocation's audit rows. Returns (request, result) parsed dicts.
    A forgery that recomputes the receipt's own digests consistently still
    fails here unless the separate audit row is forged identically too --
    and the recorded source proof is verified separately against the ledger
    and monthly receipts, which a receipt-only forgery cannot satisfy.
    """
    try:
        request = json.loads(row_request_json)
        result = json.loads(row_result_json)
    except (TypeError, ValueError) as exc:
        raise ValueError('Carry allocation evidence is malformed.') from exc
    _exact_keys(request, _REQUEST_KEYS | ({'source_selection'} if isinstance(request, dict) and 'source_selection' in request else set()), 'request')
    _exact_keys(result, _RESULT_KEYS, 'result')
    if digest(request) != row_request_digest or digest(result) != row_result_digest:
        raise ValueError('Carry allocation identity or evidence changed.')
    if (result['employee_id'] != row_employee_id or result['leave_type_id'] != row_leave_type_id
            or result['source_year'] != row_source_year or result['target_year'] != row_source_year + 1
            or result['source_digest'] != row_source_digest
            or type(result['employee_id']) is not int or type(result['leave_type_id']) is not int
            or type(result['source_year']) is not int):
        raise ValueError('Carry allocation identity or evidence changed.')
    if (request['plan'] != result or request['reviewed_digest'] != row_result_digest
            or request['actor_username'] != original_actor_username
            or type(request['actor_id']) is not int or request['actor_id'] < 1
            or not isinstance(request['session_id'], str) or not request['session_id']
            or type(request['session_version']) is not int
            or not (request['restore_epoch'] is None or isinstance(request['restore_epoch'], str))):
        raise ValueError('Carry allocation request does not bind this result.')
    _recorded_amount(result['carried_amount'], 'carried amount')
    _recorded_amount(result['held_annual'], 'held annual')
    if (result['ledger_delta'] != '0/1' or result['cash_amount'] != '0/1'
            or result['operational_spending_allowed'] is not False
            or not isinstance(result['reason'], str) or not 1 <= len(result['reason'].strip()) <= 1000
            or result['reason'] != result['reason'].strip()):
        raise ValueError('Carry allocation result evidence changed.')
    if (len(audits) != 1 or audits[0][0] != request['actor_username']
            or audits[0][1] != row_source_digest or audits[0][2] != row_result_json
            or audits[0][3] != result['reason']):
        raise ValueError('Carry allocation audit evidence changed.')
    return request, result


def _verify_recorded_source(db, *, result, employee_id, leave_type_id):
    """Bind the carried amount to the recorded source proof. No re-derivation.

    Recomputes the amount only from values recorded inside the receipt,
    cross-checked against the current ledger/monthly/attribution rows those
    recorded values name. Later unrelated or debit rows are never consulted.
    """
    completed = _exact_keys(result['source'], _COMPLETED_KEYS, 'source')
    if digest(completed) != result['source_digest']:
        raise ValueError('Carry source evidence changed.')
    _hex64(completed['selection_id'], 'source selection')
    _hex64(completed['opening_digest'], 'source opening')
    _hex64(completed['opening_apply'], 'source opening')
    _hex64(completed['live_final_source'], 'source')
    _recorded_date(completed['reviewed_on'], 'source review date')
    inner = _exact_keys(completed['source'], _CARRY_KEYS, 'source')
    if (inner['employee_id'] != employee_id or inner['leave_type_id'] != leave_type_id
            or inner['source_year'] != result['source_year'] or inner['active'] is not True
            or inner['tracks_balance'] is not True or not isinstance(inner['leave_type_code'], str)
            or _recorded_amount(inner['amount'], 'source amount') != _recorded_amount(result['carried_amount'], 'carried amount')):
        raise ValueError('Carry source identity changed.')
    _recorded_date(inner['reviewed_on'], 'source review date')
    rows = {}
    for entry in inner['rows']:
        if not isinstance(entry, list) or len(entry) != 7 or type(entry[0]) is not int or entry[0] in rows:
            raise ValueError('Carry source rows changed.')
        _recorded_amount(entry[1], 'source row amount')
        if (not isinstance(entry[2], str) or not (entry[3] is None or isinstance(entry[3], str))
                or not (entry[4] is None or (isinstance(entry[4], str) and re.fullmatch(_HEX64, entry[4])))
                or not (entry[5] is None or type(entry[5]) is int)):
            raise ValueError('Carry source rows changed.')
        _recorded_date(entry[6], 'source row release date')
        rows[entry[0]] = entry
    selected = inner['selected_ids']
    if (not isinstance(selected, list) or len(set(selected)) != len(selected)
            or any(type(item) is not int or item not in rows or rows[item][5] != result['source_year'] for item in selected)):
        raise ValueError('Carry source selection changed.')
    for ledger_id, entry in rows.items():
        ledger = db.get(LeaveBalanceTransaction, ledger_id, populate_existing=True)
        if (ledger is None or ledger.employee_id != employee_id or ledger.leave_type_id != leave_type_id
                or canonical_amount(ledger_amount(ledger)) != entry[1]
                or ledger.transaction_type != entry[2] or ledger.source_operation_key != entry[3]
                or ledger.source_digest != entry[4] or ledger.earning_year != entry[5]
                or (ledger.released_on.isoformat() if ledger.released_on else None) != entry[6]):
            raise ValueError('Carry source ledger row changed.')
    attributed = {}
    seen_attributions = set()
    for binding in inner['year_attributions']:
        if not isinstance(binding, list) or len(binding) != 3 or not isinstance(binding[0], str) or binding[0] in seen_attributions:
            raise ValueError('Carry source attribution binding changed.')
        seen_attributions.add(binding[0])
        receipt = db.get(LeaveYearAttribution, binding[0], populate_existing=True)
        if receipt is None or receipt.source_digest != binding[1] or receipt.result_digest != binding[2]:
            raise ValueError('Carry source attribution binding changed.')
        verified = verify_year_attribution(db, receipt)
        attributed[receipt.ledger_id] = dict((year, Fraction(value)) for year, value in verified['allocations'])
    attributed_values = []
    for ledger_id, entry in rows.items():
        if entry[5] is None:
            if ledger_id not in attributed:
                raise ValueError('Carry source row lacks its recorded year attribution.')
            attributed_values.append(attributed[ledger_id].get(result['source_year'], Fraction()))
    proven = set()
    monthly_ids = set()
    for binding in inner['monthly_receipts']:
        if not isinstance(binding, list) or len(binding) != 3 or not isinstance(binding[0], str) or binding[0] in monthly_ids:
            raise ValueError('Carry source monthly binding changed.')
        monthly_ids.add(binding[0])
        receipt = db.get(LeaveMonthEarning, binding[0], populate_existing=True)
        if receipt is None or receipt.employee_id != employee_id or receipt.source_digest != binding[1] or receipt.result_digest != binding[2]:
            raise ValueError('Carry source monthly binding changed.')
        verified = _verify_receipt(db, receipt)
        proven.update(item['id'] for item in verified['ledger'])
    from app.models import LeaveCarryExpiry
    from app.leave_carry_expiry import verify_expiry_receipt
    for ledger_id in selected:
        key=rows[ledger_id][3] or ''
        if key.startswith('leave_carry_expiry:'):
            receipt=db.get(LeaveCarryExpiry,key.split(':',1)[1],populate_existing=True)
            verify_expiry_receipt(db,receipt)
            if receipt.ledger_id!=ledger_id:raise ValueError('Carry exclusion source ledger changed.')
            proven.add(ledger_id)
    if any(item not in proven for item in selected):
        raise ValueError('Carry source lacks verified monthly receipt provenance.')
    verified_months = completed['verified_months']
    month_starts = set()
    for binding in verified_months:
        if not isinstance(binding, list) or len(binding) != 3 or binding[0] not in monthly_ids:
            raise ValueError('Carry completed-year monthly binding changed.')
        receipt = db.get(LeaveMonthEarning, binding[0], populate_existing=True)
        if receipt.source_digest != binding[1] or receipt.result_digest != binding[2]:
            raise ValueError('Carry completed-year monthly binding changed.')
        month_starts.add(receipt.month_start.isoformat())
    covered = completed['covered_months']
    if (not isinstance(covered, list) or any(_recorded_date(item, 'covered month') is None for item in covered)
            or sorted(covered) != sorted(month_starts) or len(set(covered)) != len(covered)):
        raise ValueError('Carry completed-year coverage changed.')
    held_total = Fraction()
    for binding in inner['held_annual']:
        if not isinstance(binding, list) or len(binding) != 2 or binding[0] not in monthly_ids:
            raise ValueError('Carry held annual binding changed.')
        held_total += _recorded_amount(binding[1], 'held annual')
    if canonical_amount(held_total) != result['held_annual']:
        raise ValueError('Carry held annual evidence changed.')
    total = exact_sum([Fraction(rows[item][1]) for item in selected] + attributed_values)
    if canonical_amount(total) != inner['amount']:
        raise ValueError('Carry source amount does not match its recorded proof.')
    return inner


@bounded_workbook_operation
def read_carry_availability(db, *, allocation_id, employee_id, leave_type_id, work_date, **identity):
    """Return verified carry availability for one receipt. Read-only.

    identity is the READER's current actor_id/session_id/session_version/
    expected_epoch, authenticated fresh by the existing guard inside _locks
    (audit-first order, exactly as the allocation writer). The original
    grant's identity is verified from the receipt's own request/audit proof
    and returned under 'original_grant'; it is never compared to the
    reader's session and never rewritten.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('Clean carry availability transaction required.')
    if (not isinstance(allocation_id, str) or re.fullmatch(_HEX64, allocation_id) is None
            or type(employee_id) is not int or employee_id < 1
            or type(leave_type_id) is not int or leave_type_id < 1
            or type(work_date) is not date):
        raise ValueError('Explicit allocation, employee, type and work date required.')
    actor = _locks(db, identity)
    return _read_verified_carry_availability(db,allocation_id=allocation_id,employee_id=employee_id,leave_type_id=leave_type_id,work_date=work_date,reader_actor_id=actor.id)


def _read_verified_carry_availability(db, *, allocation_id,employee_id,leave_type_id,work_date,reader_actor_id):
    db.execute(text('LOCK TABLE leave_carry_allocations,leave_year_attributions,leave_types IN SHARE MODE'))
    row = db.get(LeaveCarryAllocation, allocation_id, populate_existing=True)
    if row is None:
        raise ValueError('Carry allocation receipt does not exist.')
    if row.employee_id != employee_id or row.leave_type_id != leave_type_id:
        raise ValueError('Carry allocation belongs to another employee or type.')
    audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_carry_allocated',
        AuditLog.entity_type == 'leave_carry_allocation', AuditLog.entity_id == row.id)).all()
    try:
        request = json.loads(row.request_json)
        original_actor_id = request['actor_id'] if isinstance(request, dict) else None
    except (TypeError, ValueError):
        original_actor_id = None
    original = db.get(AdminAccount, original_actor_id) if type(original_actor_id) is int else None
    if original is None:
        raise ValueError('Carry allocation original actor evidence changed.')
    request, result = verify_allocation_evidence(
        row_id=row.id, row_employee_id=row.employee_id, row_leave_type_id=row.leave_type_id,
        row_source_year=row.source_year, row_request_digest=row.request_digest,
        row_source_digest=row.source_digest, row_result_digest=row.result_digest,
        row_request_json=row.request_json, row_result_json=row.result_json,
        audits=tuple((audit.actor, audit.old_value, audit.new_value, audit.reason) for audit in audits),
        original_actor_username=original.username)
    employee = db.get(Employee, employee_id, populate_existing=True)
    kind = db.get(LeaveType, leave_type_id, populate_existing=True)
    if employee is None or kind is None or not kind.active or not kind.tracks_balance:
        raise ValueError('Existing active balance-tracking employee/type required.')
    inner = _verify_recorded_source(db, result=result, employee_id=employee_id, leave_type_id=leave_type_id)
    if inner['leave_type_code'] != kind.code:
        raise ValueError('Carry source type identity changed.')
    amount = _recorded_amount(result['carried_amount'], 'carried amount')
    if kind.code == 'annual' and amount > 0:
        raise ValueError('Positive annual uses the separate source-bound cash and carry settlement service.')
    evaluation = evaluate_carry_window(carried_amount=amount,
        available_from=_recorded_date(result['available_from'], 'availability start'),
        available_through=_recorded_date(result['available_through'], 'availability end'),
        work_date=work_date)
    return dict(
        allocation_id=row.id, employee_id=employee_id, leave_type_id=leave_type_id,
        leave_type_code=kind.code, source_year=result['source_year'], target_year=result['target_year'],
        source_digest=result['source_digest'], source_amount=result['carried_amount'],
        held_annual=result['held_annual'],
        available_from=result['available_from'], available_through=result['available_through'],
        work_date=work_date.isoformat(), kind=evaluation['kind'], within_window=evaluation['within_window'],
        eligible=evaluation['eligible'],
        available_grant_ceiling=canonical_amount(evaluation['available_grant_ceiling']),
        preserved_unavailable=canonical_amount(evaluation['preserved_unavailable']),
        debt_amount=canonical_amount(evaluation['debt_amount']),
        posting_allowed=False, operational_spending_allowed=False,
        is_remaining_spendable=False, remaining_spendable=None,
        consumption_provenance='unresolved', unresolved=UNRESOLVED,
        original_grant=dict(actor_id=request['actor_id'], actor_username=request['actor_username'],
            session_id=request['session_id'], session_version=request['session_version'],
            restore_epoch=request['restore_epoch']),
        read_by_actor_id=reader_actor_id)
