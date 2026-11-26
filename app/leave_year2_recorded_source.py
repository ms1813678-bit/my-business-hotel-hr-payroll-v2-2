"""Isolated, versioned later-year RECORDED source proof (candidate). Not wired anywhere.

The old completed-year verifier (leave_carry_availability._verify_recorded_source) is untouched and
is used unchanged for the OWN year-tagged rows. This module adds the missing piece: the usage of the
source year that was attributed to an EARLIER grant, recorded as named consumption receipts.

Recorded proof (version year2-recorded-v1) names: the own completed-year source (old exact shape,
amount == own year-tagged rows), and each consumption receipt (id, source/result digests, grant kind,
id, grant year, grant ceiling, usage ledger id and amount, work date, quantity). Verification checks
every named row/proof against the stored immutable consumption audit proof and recomputes the
amount from recorded values only. It never re-derives from today's live state and never scans
later history, so genuinely later history growth is ignored while every named row is verified.
No ledger write, no policy, no cash. posting_allowed is always False. Source amount, not a balance.
"""
from datetime import date
from fractions import Fraction
import json
import re

from sqlalchemy import select

from app.leave_carry_availability import _verify_recorded_source
from app.leave_carry_consumption import _grant, _verify_recorded
from app.leave_exact_amount import as_fraction, canonical_amount, ledger_amount
from app.leave_month_earning import CATEGORIES, _verify_receipt, digest
from app.leave_year2_completed_shape import COMPLETED_KEYS
from app.leave_year2_net_source import review_year2_net_source
from app.models import LeaveMonthEarning, LeaveRegisterEntry, LeaveType, LeaveAnnualSettlement, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveCarryConsumption

VERSION = 'year2-recorded-v1'
PROOF_KEYS = {'held_annual', 'version', 'employee_id', 'leave_type_id', 'source_year', 'own', 'own_amount', 'consumptions',
              'attributed_amount', 'net_amount'}
CONSUMPTION_KEYS = {'id', 'source_digest', 'result_digest', 'grant_kind', 'grant_id', 'grant_year', 'grant_ceiling',
                    'usage_ledger_id', 'usage_amount', 'work_date', 'quantity'}
_HEX = '[0-9a-f]{64}'


def _amount(value, label):
    if not isinstance(value, str) or re.fullmatch(r'-?[0-9]+/[0-9]+', value) is None:
        raise ValueError(f'Recorded {label} must be a canonical amount.')
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f'Recorded {label} must be a canonical amount.') from exc
    if canonical_amount(parsed) != value:
        raise ValueError(f'Recorded {label} must be a canonical amount.')
    return parsed


def verify_year2_recorded_source(db, *, stored, employee_id, leave_type_id):
    """stored = dict(source=proof, source_digest, source_year, carried_amount). Returns the verified net."""
    if (not isinstance(stored, dict) or set(stored) != {'source', 'source_digest', 'source_year', 'carried_amount'}
            or not isinstance(stored['source'], dict) or set(stored['source']) != PROOF_KEYS):
        raise ValueError('Year-2 recorded source keys changed.')
    proof = stored['source']
    if digest(proof) != stored['source_digest'] or proof['version'] != VERSION:
        raise ValueError('Year-2 recorded source evidence changed.')
    year = stored['source_year']
    if (proof['employee_id'] != employee_id or proof['leave_type_id'] != leave_type_id or type(year) is not int
            or proof['source_year'] != year or type(employee_id) is not int or type(leave_type_id) is not int):
        raise ValueError('Year-2 recorded source identity changed.')
    own_amount = _amount(proof['own_amount'], 'own amount')
    attributed = _amount(proof['attributed_amount'], 'attributed amount')
    net = _amount(proof['net_amount'], 'net amount')
    if net != own_amount + attributed or _amount(stored['carried_amount'], 'carried amount') != net:
        raise ValueError('Year-2 recorded net does not equal own rows plus attributed usage.')
    own = proof['own']
    if not isinstance(own, dict) or set(own) != set(COMPLETED_KEYS):
        raise ValueError('Year-2 own source must keep the exact completed-year keys.')
    inner = _verify_recorded_source(db, result=dict(source=own, source_digest=digest(own), source_year=year,
        carried_amount=canonical_amount(own_amount), held_annual=proof['held_annual']), employee_id=employee_id, leave_type_id=leave_type_id)
    selected = set(inner['selected_ids'])
    seen, grant_used, day_used, row_used, total = set(), {}, {}, {}, Fraction()
    if not isinstance(proof['consumptions'], list):
        raise ValueError('Year-2 consumption bindings changed.')
    for binding in proof['consumptions']:
        if not isinstance(binding, dict) or set(binding) != CONSUMPTION_KEYS or binding['id'] in seen:
            raise ValueError('Year-2 consumption binding changed or duplicated.')
        if (not isinstance(binding['id'], str) or re.fullmatch(_HEX, binding['id']) is None
                or any(not isinstance(binding[k], str) or re.fullmatch(_HEX, binding[k]) is None
                       for k in ('source_digest', 'result_digest', 'grant_id'))
                or type(binding['usage_ledger_id']) is not int or binding['grant_kind'] not in {'carry', 'annual'}
                or not isinstance(binding['work_date'], str)):
            raise ValueError('Year-2 consumption binding types changed.')
        try:
            if date.fromisoformat(binding['work_date']).isoformat() != binding['work_date']:
                raise ValueError
        except ValueError as exc:
            raise ValueError('Year-2 consumption work date changed.') from exc
        seen.add(binding['id'])
        row = db.get(LeaveCarryConsumption, binding['id'], populate_existing=True)
        if (row is None or row.employee_id != employee_id or row.leave_type_id != leave_type_id
                or row.source_digest != binding['source_digest'] or row.result_digest != binding['result_digest']
                or row.grant_kind != binding['grant_kind'] or row.grant_id != binding['grant_id']
                or row.usage_ledger_id != binding['usage_ledger_id'] or row.work_date.isoformat() != binding['work_date']
                or row.quantity_canonical != binding['quantity']):
            raise ValueError('Year-2 consumption receipt changed.')
        _request, saved = _verify_recorded(db, row)        # immutable audit proof, digests, no re-derive
        plan = saved['plan']
        qty = _amount(binding['quantity'], 'quantity')
        ceiling = _amount(binding['grant_ceiling'], 'grant ceiling')
        usage_amount = _amount(binding['usage_amount'], 'usage amount')
        if (not 0 < qty <= 1 or plan['grant']['ceiling'] != binding['grant_ceiling'] or plan['usage']['amount'] != binding['usage_amount']
                or plan['usage']['ledger_id'] != binding['usage_ledger_id']):
            raise ValueError('Year-2 consumption plan evidence changed.')
        if binding['usage_ledger_id'] not in selected:
            raise ValueError('Consumed usage is not a year-tagged row of the source year.')
        ledger = db.get(LeaveBalanceTransaction, binding['usage_ledger_id'], populate_existing=True)
        if (ledger is None or ledger.employee_id != employee_id or ledger.leave_type_id != leave_type_id
                or ledger.earning_year != year or as_fraction(ledger_amount(ledger)) != -usage_amount):
            raise ValueError('Consumed usage ledger row changed.')
        usage = plan['usage']
        work_date = row.work_date
        month = db.get(LeaveMonthEarning, usage['monthly_receipt_id'], populate_existing=True)
        kind = db.get(LeaveType, leave_type_id, populate_existing=True)
        code = next((c for c, v in CATEGORIES.items() if kind is not None and v == kind.code), None)
        entry = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == employee_id,
            LeaveRegisterEntry.work_date == work_date))
        # Historic usage semantics on named rows only (the live _usage also needs the CURRENT epoch head, so it is not reused):
        # actual own month receipt, month == work-date month, category code of this type, register-day code, one usage item.
        if (month is None or month.employee_id != employee_id or month.month_start != work_date.replace(day=1)
                or month.source_digest != usage['source_digest'] or month.result_digest != usage['result_digest']
                or code is None or usage['code'] != code or usage['work_date'] != work_date.isoformat()
                or usage['employee_id'] != employee_id or usage['leave_type_id'] != leave_type_id
                or entry is None or entry.code != code or kind is None or not kind.tracks_balance
                or sum(1 for item in _verify_receipt(db, month)['ledger']
                       if item['id'] == binding['usage_ledger_id'] and item['kind'] == 'usage' and item['category'] == code) != 1):
            raise ValueError('Consumed usage month, category or register-day binding changed.')
        model = LeaveCarryAllocation if binding['grant_kind'] == 'carry' else LeaveAnnualSettlement
        grant = db.get(model, binding['grant_id'], populate_existing=True) if binding['grant_kind'] in {'carry', 'annual'} else None
        if (grant is None or grant.employee_id != employee_id or grant.leave_type_id != leave_type_id
                or type(binding['grant_year']) is not int or grant.source_year != binding['grant_year']
                or binding['grant_year'] >= year):
            raise ValueError('Consumed grant identity or year changed; a same-year or later grant cannot net the year.')
        # Real grant semantics (same function the consumption service used): carry = receipt + audit + recorded source
        # proof + window; annual = closing receipt + recorded pre-cash source + rule + held-release proof. Evaluated on
        # the RECORDED work date. Epoch-free (the identity passed only echoes the original actor). Expiry not consulted.
        live = _grant(db, grant_kind=binding['grant_kind'], grant_id=binding['grant_id'], employee_id=employee_id,
            leave_type_id=leave_type_id, work_date=row.work_date, identity=dict(actor_id=_request['identity']['actor_id']))
        if (live != plan['grant'] or live['source_digest'] != grant.source_digest
                or Fraction(live['ceiling']) != ceiling):
            raise ValueError('Consumed grant live proof differs from the recorded grant.')
        key = (binding['grant_kind'], binding['grant_id'])
        grant_used[key] = grant_used.get(key, Fraction()) + qty
        day_used[binding['work_date']] = day_used.get(binding['work_date'], Fraction()) + qty
        row_used[binding['usage_ledger_id']] = row_used.get(binding['usage_ledger_id'], Fraction()) + qty
        if grant_used[key] > ceiling:
            raise ValueError('Recorded attribution exceeds grant cap.')
        if day_used[binding['work_date']] > 1:
            raise ValueError('Recorded attribution exceeds day cap.')
        if row_used[binding['usage_ledger_id']] > usage_amount:
            raise ValueError('Recorded attribution exceeds usage-row cap.')
        total += qty
    if total != attributed:
        raise ValueError('Year-2 attributed amount does not equal its named consumption receipts.')
    return dict(net=net, own_amount=own_amount, attributed=attributed, consumptions=len(seen),
                posting_allowed=False, semantic='read_only_source_not_available_balance')


def build_year2_recorded_source(db, **selection):
    """Build the versioned proof from the live verified net, then prove it with the recorded verifier."""
    net = review_year2_net_source(db, **selection)
    parts = net['completed_parts']
    own = dict(source=parts['carry_payload'], selection_id=net['source']['selection_id'],
        opening_digest=parts['opening_digest'], opening_apply=parts['opening_apply'],
        covered_months=parts['covered_months'], verified_months=parts['verified_months'],
        live_final_source=parts['live_final_source'], current_tiers=parts['current_tiers'],
        reviewed_on=selection['reviewed_on'].isoformat())
    bindings = []
    for consumption_id, source_digest, result_digest, grant_year in net['source']['attributions']:
        row = db.get(LeaveCarryConsumption, consumption_id, populate_existing=True)
        _request, saved = _verify_recorded(db, row)
        plan = saved['plan']
        bindings.append(dict(id=row.id, source_digest=row.source_digest, result_digest=row.result_digest,
            grant_kind=row.grant_kind, grant_id=row.grant_id, grant_year=grant_year,
            grant_ceiling=plan['grant']['ceiling'], usage_ledger_id=row.usage_ledger_id,
            usage_amount=plan['usage']['amount'], work_date=row.work_date.isoformat(), quantity=row.quantity_canonical))
    proof = dict(held_annual=canonical_amount(parts['held_annual']), version=VERSION, employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'],
        source_year=selection['source_year'], own=own, own_amount=canonical_amount(net['own_rows_amount']),
        consumptions=bindings, attributed_amount=canonical_amount(net['attributed']), net_amount=canonical_amount(net['amount']))
    stored = json.loads(json.dumps(dict(source=proof, source_digest=digest(proof), source_year=selection['source_year'],
        carried_amount=canonical_amount(net['amount']))))
    verify_year2_recorded_source(db, stored=stored, employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'])
    return dict(stored=stored, amount=net['amount'], held_annual=parts['held_annual'], posting_allowed=False,
        year2_net_source_digest=net['source_digest'], source_verified=True, unresolved=())
