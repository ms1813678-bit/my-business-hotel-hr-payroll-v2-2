# ruff: noqa: F811
"""Proof-level year 3: a 2027 source (12 real months, 2028 clock) that consumes the REAL 2026 annual grant, then v1/v2
proof through the real versioned router. The 2027 transition admission is an explicit TEST-ONLY scaffold (the next-year
transition consumer is not admitted); this is NOT a fully admitted year 3 and not operational."""
from calendar import monthrange
from datetime import date
import json
from fractions import Fraction

import pytest

from app.models import LeaveMonthEarning, LeaveRegisterEntry
from app.leave_month_earning import review_month_earning, apply_month_earning
from app.leave_carry_consumption import review_carry_consumption, record_carry_consumption
from app.leave_year2_proof_guard import active_stack
from app.leave_year2_recorded_source import build_year2_recorded_source
from app.leave_year2_recorded_source_v2 import build_year2_recorded_source_v2, verify_year_proof
from app.leave_recorded_source_versions import verify_versioned_recorded_source
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import FULL
from tests.test_year2_annual_expiry_shape import settled_2026, Clock2028


def year3(prior_year, monkeypatch):
    db, args = settled_2026(prior_year, monkeypatch)
    for mod in ('app.leave_month_earning', 'app.leave_carry_source', 'app.leave_carry_expiry', 'app.leave_completed_year_source',
                'app.leave_year2_net_source', 'app.leave_carry_consumption', 'app.leave_year_transition'):
        try:
            monkeypatch.setattr(mod + '.datetime', Clock2028)
        except AttributeError:
            pass
    import app.leave_year_transition as transition
    original = transition.verify_transition_for_month
    scaffold = lambda db_, **kw: [] if kw['month_start'].year == 2027 else original(db_, **kw)   # TEST-ONLY scaffold
    monkeypatch.setattr(transition, 'verify_transition_for_month', scaffold)
    import app.leave_year2_net_source as net_module
    monkeypatch.setattr(net_module, 'verify_transition_for_month', scaffold)
    emp, kind = args['employee_id'], args['leave_type_id']
    for month in range(1, 13):
        for day in range(1, monthrange(2027, month)[1] + 1):
            db.add(LeaveRegisterEntry(employee_id=emp, work_date=date(2027, month, day),
                code='A' if (month, day) == (1, 3) else '', source='synthetic', recorded_by='synthetic'))
    db.flush()
    ident = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    for month in range(1, 13):
        months = [date(2027, m, 1) for m in range(1, month + 1)]
        call = dict(selection_id=args['selection_id'], month_start=months[-1],
            evaluated_on=date(2027, month + 1, 1) if month < 12 else date(2028, 1, 1),
            current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **ident)
        reviewed = review_month_earning(db, **call)
        apply_month_earning(db, operation_id=format(800 + month, '064x'), reviewed_digest=reviewed['reviewed_digest'],
            human_confirmed=True, reason='Synthetic year 3 month', **call)
    month = db.get(LeaveMonthEarning, format(801, '064x'))
    usage = next(i for i in json.loads(month.result_json)['ledger'] if i['category'] == 'A' and i['kind'] == 'usage')
    sel = dict(grant_kind='annual', grant_id='9' * 64, employee_id=emp, leave_type_id=kind, monthly_receipt_id=month.id,
        usage_ledger_id=usage['id'], work_date=date(2027, 1, 3), quantity=Fraction(1), reason='Synthetic year 3 consumption', **ident)
    review = review_carry_consumption(db, **sel)
    record_carry_consumption(db, operation_id='e' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **sel)
    months = [date(2027, m, 1) for m in range(1, 13)]
    args3 = dict(employee_id=emp, leave_type_id=kind, source_year=2027, selection_id=args['selection_id'],
        reviewed_on=date(2028, 1, 2), current_insurance_years={(emp, m): None for m in months},
        age_evaluation_dates={m: m for m in months}, **ident)
    return db, args3


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year3_v1_and_v2_proof_through_router_over_real_2026_grant(prior_year, monkeypatch):
    import app.leave_year2_proof_guard as guard
    import app.leave_year2_recorded_source_v2 as v2mod
    db, args = year3(prior_year, monkeypatch)
    e, t = args['employee_id'], args['leave_type_id']
    built = build_year2_recorded_source(db, **args)
    stored = json.loads(json.dumps(built['stored']))
    assert stored['source']['source_year'] == 2027 and stored['source']['consumptions']
    assert all(c['grant_kind'] == 'annual' and c['grant_year'] == 2026 for c in stored['source']['consumptions'])
    pushes = []
    real = guard.guarded
    def record(node, fn):
        pushes.append((len(guard.active_stack()), node[2:]))
        return real(node, fn)
    monkeypatch.setattr(guard, 'guarded', record)
    monkeypatch.setattr(v2mod, 'guarded', record)
    assert verify_versioned_recorded_source(db, result=dict(stored, held_annual=stored['source']['held_annual']),
                                            employee_id=e, leave_type_id=t)
    print('YEAR3 V1 PUSH CHAIN', pushes)
    years = [y for _, (y, _l) in pushes]
    assert 2027 in years and 2026 in years and max(d for d, _ in pushes) >= 2     # real 2027 -> 2026 grant -> 2025 chain
    assert active_stack() == ()
    # 2027 has no cash/zero debit yet, so its v2 has no allocations; build and verify through both entries.
    out = build_year2_recorded_source_v2(db, **args)
    v2 = json.loads(json.dumps(out['stored']))
    assert v2['source']['allocations'] == []
    pushes.clear()
    assert verify_year_proof(db, stored=v2, employee_id=e, leave_type_id=t)['net'] == Fraction(v2['source']['net_amount'])
    print('YEAR3 V2 ENTRY PUSH CHAIN', pushes)
    assert any(y==2027 and level==1 for _,(y,level) in pushes)
    pushes.clear()
    assert verify_versioned_recorded_source(db, result=dict(v2, held_annual=v2['source']['held_annual']), employee_id=e, leave_type_id=t)
    assert active_stack() == ()
    print('YEAR3 V2 ROUTER PUSH CHAIN', pushes)
    assert any(y==2027 and level==1 for _,(y,level) in pushes)
