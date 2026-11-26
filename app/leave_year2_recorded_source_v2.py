"""Isolated NEW-VERSION recorded source proof: year-Y source read AFTER Y's cash or zero debit exists. Not wired.

Version year2-recorded-v1 (leave_year2_recorded_source.py) is unchanged. The old completed-year verifier
(leave_carry_availability._verify_recorded_source) is unchanged and cannot prove a selected `leave_cashout:` or
`leave_zero:` row (it proves monthly and expiry rows only). So version v2 splits the own source:

  base        = a complete v1 stored proof whose own part is a PRE-ALLOCATION view in the exact old shape: the cash and
                zero rows are removed from rows/selected_ids and the amount is exactly the sum of what remains (+ year
                attributions), so the unchanged old verifier proves it. Prior-grant attribution stays in base, once.
  allocations = each removed row, named: kind, ledger id, receipt id, canonical negative amount, receipt digests.
                Proven with the SAME functions the live source uses (verify_annual_cash_allocation,
                verify_zero_ledger_for_year) on that named row only. No source re-derive.
  net         = base net + sum(allocations), exactly once. Builder refuses any split that does not equal the live net.

Dependency graph of a proof of year Y: grants of years < Y (v1 bindings) and same-year receipts that are leaves (their
recorded plans hold the pre-cash source, never a proof). verify_year_proof() is the version-selecting entry; it runs under
leave_year2_proof_guard (ContextVar stack, strictly decreasing (year, level) rank, depth budget) which the versioned router also uses. No ledger write, no policy, no cash.
posting_allowed is always False. A source amount, not a balance.
"""
from fractions import Fraction
import json
import re

from app.leave_exact_amount import as_fraction, canonical_amount, ledger_amount
from app.leave_month_earning import digest
from app.leave_year2_net_source import review_year2_net_source
from app.leave_year2_proof_guard import guarded
from app.leave_year2_recorded_source import VERSION as V1, _amount, verify_year2_recorded_source
from app.models import LeaveAnnualSettlement, LeaveBalanceTransaction, LeaveCashOut, LeaveCarryConsumption, LeaveZeroDecision

VERSION = 'year2-recorded-v2'
PROOF_KEYS = {'version', 'employee_id', 'leave_type_id', 'source_year', 'base', 'allocations', 'allocation_amount',
              'net_amount', 'held_annual'}
ALLOCATION_KEYS = {'kind', 'ledger_id', 'receipt_id', 'amount', 'annual_id', 'source_digest', 'result_digest'}
_HEX = '[0-9a-f]{64}'
PREFIX = {'cash': 'leave_cashout:', 'zero': 'leave_zero:'}
KNOWN_NON_ALLOCATION = ('month_earning:', 'month_correction:', 'leave_carry_expiry:')


def split_allocations(own):
    """Pure. own = completed-year own shape (as built from the live net). Returns (pre_allocation_own, allocations).

    Exhaustive: every selected row is a month-earning row, an expiry row, a cash row or a zero row; anything else refuses.
    """
    inner = own['source']
    rows = {entry[0]: entry for entry in inner['rows']}
    allocations, removed = [], set()
    for ledger_id in inner['selected_ids']:
        entry = rows.get(ledger_id)
        if entry is None:
            raise ValueError('Selected row is missing from the recorded rows.')
        key = entry[3] or ''
        kind = next((k for k, prefix in PREFIX.items() if key.startswith(prefix)), None)
        if kind is None:
            if not key.startswith(KNOWN_NON_ALLOCATION):
                raise ValueError('Unknown selected row kind refuses the year split.')
            continue
        if ledger_id in removed:
            raise ValueError('Duplicate selected row.')
        removed.add(ledger_id)
        allocations.append(dict(kind=kind, ledger_id=ledger_id, receipt_id=key.split(':', 1)[1], amount=entry[1],
            annual_id=key.split(':', 1)[1] if kind == 'cash' else None, source_digest=None, result_digest=None))
    pre = copy_own(own)
    pre['source']['rows'] = [entry for entry in inner['rows'] if entry[0] not in removed]
    pre['source']['selected_ids'] = [i for i in inner['selected_ids'] if i not in removed]
    total = sum((Fraction(a['amount']) for a in allocations), Fraction())
    pre['source']['amount'] = canonical_amount(Fraction(inner['amount']) - total)
    return pre, allocations


def copy_own(own):
    return json.loads(json.dumps(own))


def _check_allocations(db, allocations, *, employee_id, leave_type_id, year):
    from app.leave_annual_settlement_store import verify_annual_cash_allocation
    from app.leave_zero_decision import verify_zero_ledger_for_year
    if not isinstance(allocations, list):
        raise ValueError('Year allocations changed.')
    seen_ledger, seen_receipt, total = set(), set(), Fraction()
    for item in allocations:
        if not isinstance(item, dict) or set(item) != ALLOCATION_KEYS or item['kind'] not in PREFIX:
            raise ValueError('Year allocation binding changed.')
        if (type(item['ledger_id']) is not int or not isinstance(item['receipt_id'], str)
                or re.fullmatch(_HEX, item['receipt_id']) is None
                or item['ledger_id'] in seen_ledger or item['receipt_id'] in seen_receipt):
            raise ValueError('Year allocation binding changed or duplicated.')
        seen_ledger.add(item['ledger_id'])
        seen_receipt.add(item['receipt_id'])
        amount = _amount(item['amount'], 'allocation amount')
        ledger = db.get(LeaveBalanceTransaction, item['ledger_id'], populate_existing=True)
        if (ledger is None or ledger.employee_id != employee_id or ledger.leave_type_id != leave_type_id
                or ledger.earning_year != year or amount >= 0 or as_fraction(ledger_amount(ledger)) != amount
                or ledger.source_operation_key != PREFIX[item['kind']] + item['receipt_id']):
            raise ValueError('Year allocation ledger row changed.')
        if item['kind'] == 'cash':
            cash = db.get(LeaveCashOut, item['receipt_id'], populate_existing=True)
            annual = db.get(LeaveAnnualSettlement, item['receipt_id'], populate_existing=True)
            if (cash is None or annual is None or item['annual_id'] != item['receipt_id']
                    or annual.cash_out_id != item['receipt_id'] or cash.ledger_id != item['ledger_id']
                    or annual.employee_id != employee_id or annual.leave_type_id != leave_type_id
                    or annual.source_year != year or json.loads(cash.source_json).get('source_year') != year
                    or annual.source_digest != item['source_digest'] or annual.result_digest != item['result_digest']):
                raise ValueError('Year cash allocation receipt changed.')
            verify_annual_cash_allocation(db, annual, cash)
        else:
            zero = db.get(LeaveZeroDecision, item['receipt_id'], populate_existing=True)
            if (zero is None or item['annual_id'] is not None or zero.ledger_id != item['ledger_id']
                    or zero.source_digest != item['source_digest'] or zero.result_digest != item['result_digest']):
                raise ValueError('Year zero allocation receipt changed.')
            verify_zero_ledger_for_year(db, ledger, employee_id=employee_id, leave_type_id=leave_type_id, source_year=year)
        total += amount
    return total


def _check_partition(proof, year):
    try:
        _check_partition_unsafe(proof, year)
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ValueError('Ledger partition: malformed proof shape.') from None


def _check_partition_unsafe(proof, year):
    """Base and allocation ledger/receipt sets are disjoint, unique, and base carries no year-Y cash/zero row."""
    base_inner = proof['base']['source']['own']['source']
    allocations = proof['allocations']
    if not isinstance(allocations, list) or any(not isinstance(a, dict) for a in allocations):
        raise ValueError('Year allocations changed.')
    alloc_ledger = [a.get('ledger_id') for a in allocations]
    alloc_receipt = [a.get('receipt_id') for a in allocations]
    if len(set(alloc_ledger)) != len(alloc_ledger) or len(set(alloc_receipt)) != len(alloc_receipt):
        raise ValueError('Ledger partition: allocation ids must be unique.')
    base_rows = {entry[0] for entry in base_inner['rows']}
    base_selected = set(base_inner['selected_ids'])
    usage = {c['usage_ledger_id'] for c in proof['base']['source']['consumptions']}
    if set(alloc_ledger) & (base_rows | base_selected | usage):
        raise ValueError('Ledger partition: an allocation row also appears in the base set.')
    if set(alloc_receipt) & {entry[3].split(':', 1)[1] for entry in base_inner['rows']
                             if isinstance(entry[3], str) and ':' in entry[3]}:
        raise ValueError('Ledger partition: an allocation receipt also appears in the base set.')
    for entry in base_inner['rows']:
        if (entry[5] == year and isinstance(entry[3], str) and entry[3].startswith(tuple(PREFIX.values()))):
            raise ValueError('Ledger partition: base carries a same-year cash or zero row.')
    if any(i not in base_rows for i in base_selected):
        raise ValueError('Ledger partition: base selected row missing from base rows.')


def verify_year2_recorded_source_v2_unguarded(db, *, stored, employee_id, leave_type_id):
    """INTERNAL: runs under a guard frame already pushed by the caller (router or verify_year_proof). Do not call directly."""
    if (not isinstance(stored, dict) or set(stored) != {'source', 'source_digest', 'source_year', 'carried_amount'}
            or not isinstance(stored['source'], dict) or set(stored['source']) != PROOF_KEYS):
        raise ValueError('Year-2 v2 source keys changed.')
    proof = stored['source']
    if digest(proof) != stored['source_digest'] or proof['version'] != VERSION:
        raise ValueError('Year-2 v2 source evidence changed.')
    year = stored['source_year']
    if (type(year) is not int or proof['source_year'] != year or proof['employee_id'] != employee_id
            or proof['leave_type_id'] != leave_type_id or type(employee_id) is not int or type(leave_type_id) is not int):
        raise ValueError('Year-2 v2 source identity changed.')
    base = proof['base']
    if (not isinstance(base, dict) or not isinstance(base.get('source'), dict) or base['source'].get('version') != V1
            or base.get('source_year') != year or base['source']['held_annual'] != proof['held_annual']):
        raise ValueError('Year-2 v2 base must be a v1 proof of the same year.')
    _check_partition(proof, year)
    base_result = verify_year_proof(db, stored=base, employee_id=employee_id, leave_type_id=leave_type_id)
    allocated = _check_allocations(db, proof['allocations'], employee_id=employee_id, leave_type_id=leave_type_id, year=year)
    if _amount(proof['allocation_amount'], 'allocation total') != allocated:
        raise ValueError('Year allocation total changed.')
    net = _amount(proof['net_amount'], 'net amount')
    if net != base_result['net'] + allocated or _amount(stored['carried_amount'], 'carried amount') != net:
        raise ValueError('Year-2 v2 net must equal base net plus its named allocations exactly once.')
    return dict(net=net, base_net=base_result['net'], allocations=len(proof['allocations']), allocation_amount=allocated,
                posting_allowed=False, semantic='read_only_source_not_available_balance')


def verify_year2_recorded_source_v2(db, *, stored, employee_id, leave_type_id):
    """Public guarded surface (same as verify_year_proof for v2 input). A v1 or unknown version is refused here."""
    if not isinstance(stored, dict) or not isinstance(stored.get('source'), dict) or stored['source'].get('version') != VERSION:
        raise ValueError('Year-2 v2 source keys changed.')
    return verify_year_proof(db, stored=stored, employee_id=employee_id, leave_type_id=leave_type_id)


def verify_year_proof(db, *, stored, employee_id, leave_type_id):
    """Guarded version-selecting entry. Pushes (employee, type, year, level) on the active stack for the whole verification."""
    year = stored.get('source_year') if isinstance(stored, dict) else None
    version = stored['source'].get('version') if isinstance(stored.get('source'), dict) else None
    if version == V1:
        level, fn = 0, verify_year2_recorded_source
    elif version == VERSION:
        level, fn = 1, verify_year2_recorded_source_v2_unguarded
    else:
        raise ValueError('Unknown proof version.')
    return guarded((employee_id, leave_type_id, year, level),
                   lambda: fn(db, stored=stored, employee_id=employee_id, leave_type_id=leave_type_id))


def build_year2_recorded_source_v2(db, **selection):
    net = review_year2_net_source(db, **selection)
    parts = net['completed_parts']
    own = dict(source=parts['carry_payload'], selection_id=net['source']['selection_id'],
        opening_digest=parts['opening_digest'], opening_apply=parts['opening_apply'],
        covered_months=parts['covered_months'], verified_months=parts['verified_months'],
        live_final_source=parts['live_final_source'], current_tiers=parts['current_tiers'],
        reviewed_on=selection['reviewed_on'].isoformat())
    pre, split = split_allocations(own)
    allocations = []
    for item in split:
        if item['kind'] == 'cash':
            annual = db.get(LeaveAnnualSettlement, item['receipt_id'], populate_existing=True)
            if annual is None:
                raise ValueError('Cash allocation settlement missing.')
            item['source_digest'], item['result_digest'] = annual.source_digest, annual.result_digest
        else:
            zero = db.get(LeaveZeroDecision, item['receipt_id'], populate_existing=True)
            if zero is None:
                raise ValueError('Zero allocation receipt missing.')
            item['source_digest'], item['result_digest'] = zero.source_digest, zero.result_digest
        allocations.append(item)
    allocated = sum((Fraction(a['amount']) for a in allocations), Fraction())
    pre_amount = Fraction(pre['source']['amount'])
    attributed = as_fraction(net['attributed'])
    if pre_amount + allocated != as_fraction(net['own_rows_amount']) or pre_amount + attributed + allocated != as_fraction(net['amount']):
        raise ValueError('Split omits or double counts: base plus allocations must equal the live net exactly once.')
    bindings = []
    for consumption_id, source_digest, result_digest, grant_year in net['source']['attributions']:
        row = db.get(LeaveCarryConsumption, consumption_id, populate_existing=True)
        from app.leave_carry_consumption import _verify_recorded
        _request, saved = _verify_recorded(db, row)
        plan = saved['plan']
        bindings.append(dict(id=row.id, source_digest=row.source_digest, result_digest=row.result_digest,
            grant_kind=row.grant_kind, grant_id=row.grant_id, grant_year=grant_year, grant_ceiling=plan['grant']['ceiling'],
            usage_ledger_id=row.usage_ledger_id, usage_amount=plan['usage']['amount'], work_date=row.work_date.isoformat(),
            quantity=row.quantity_canonical))
    held = canonical_amount(parts['held_annual'])
    base_proof = dict(held_annual=held, version=V1, employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'],
        source_year=selection['source_year'], own=pre, own_amount=canonical_amount(pre_amount), consumptions=bindings,
        attributed_amount=canonical_amount(attributed), net_amount=canonical_amount(pre_amount + attributed))
    base = dict(source=base_proof, source_digest=digest(base_proof), source_year=selection['source_year'],
        carried_amount=canonical_amount(pre_amount + attributed))
    proof = dict(version=VERSION, employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'],
        source_year=selection['source_year'], base=base, allocations=allocations, allocation_amount=canonical_amount(allocated),
        net_amount=canonical_amount(net['amount']), held_annual=held)
    stored = json.loads(json.dumps(dict(source=proof, source_digest=digest(proof), source_year=selection['source_year'],
        carried_amount=canonical_amount(net['amount']))))
    verify_year_proof(db, stored=stored, employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'])
    return dict(stored=stored, amount=net['amount'], held_annual=parts['held_annual'], posting_allowed=False,
                year2_net_source_digest=net['source_digest'], source_verified=True, unresolved=())
