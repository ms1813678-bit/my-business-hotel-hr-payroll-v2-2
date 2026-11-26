# ruff: noqa: F811
"""Real consumer dispatch and real transition admission, no routing monkeypatch.
Two fixtures: zero prior attribution and real 9/7 prior-grant attribution.
"""
from datetime import date, datetime
import pytest
from app.models import CAIRO_TZ
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import FULL, full_year, zero_year, Clock, IDENT
from tests.test_leave_cash_out import payroll


def stage(name, fn):
    try:
        out = fn()
        print('STAGE', name, 'OK')
        return out
    except Exception as exc:
        print('STAGE', name, 'FAIL', type(exc).__name__, str(exc)[:200])
        raise


@pytest.mark.parametrize('attributed', [False, True])
@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_2026_closing_to_transition_to_jan_2027(prior_year, monkeypatch, attributed):
    import app.leave_year_transition as trans_m
    from app.leave_zero_decision import record_zero_decision, review_zero_decision
    from app.leave_carry_allocation_store import record_carry_allocation, review_carry_allocation
    from app.leave_annual_settlement_store import apply_annual_settlement, review_annual_settlement_apply
    from app.leave_year_transition import record_year_transition, review_year_transition
    monkeypatch.setattr('app.leave_cash_out.datetime', Clock)
    db, args = (full_year if attributed else zero_year)(prior_year, monkeypatch)
    emp = args['employee_id']
    kinds = prior_year[2]
    sources = {c: {**args, 'leave_type_id': kinds[c]} for c in ('A', 'W', 'H')}
    monkeypatch.setattr('app.leave_carry_expiry.datetime', Clock)
    from app.leave_carry_expiry import apply_carry_expiry, review_carry_expiry
    from sqlalchemy import select
    from app.models import LeaveAnnualSettlement
    old = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.source_year == 2025))
    ex = dict(grant_kind='annual', grant_id=old.id, employee_id=emp, leave_type_id=kinds['A'], evaluated_on=date(2027, 1, 2),
        reason='Synthetic explicit old annual expiry', **{k: args[k] for k in IDENT})
    rx = stage('expiry_2025_A_review', lambda: review_carry_expiry(db, **ex))
    stage('expiry_2025_A_apply', lambda: apply_carry_expiry(db, operation_id='8' * 64, reviewed_digest=rx['reviewed_digest'], human_confirmed=True, **ex))
    # annual cash (real settlement; the annual review already uses the year-2 builder)
    _run, item = payroll(db, prior_year[1], start=date(2026, 12, 1), end=date(2026, 12, 31), status='draft')
    sel = dict(source_selection=sources['A'], full_annual_entitlement=21, payroll_item_id=item.id,
        available_from=date(2027, 1, 2), available_through=date(2027, 4, 30), reason='Synthetic 2026 annual close')
    rv = stage('annual_review', lambda: review_annual_settlement_apply(db, **sel))
    stage('annual_apply', lambda: apply_annual_settlement(db, operation_id='5' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, **sel))
    zr = stage('zero_W_review', lambda: review_zero_decision(db, source_selection=sources['W'], reason='Synthetic 2026 zero W'))
    stage('zero_W_record', lambda: record_zero_decision(db, operation_id='6' * 64, reviewed_digest=zr['reviewed_digest'], human_confirmed=True,
        source_selection=sources['W'], reason='Synthetic 2026 zero W'))
    csel = dict(source_selection=sources['H'], available_from=None, available_through=None, reason='Synthetic 2026 H closing')
    cr = stage('carry_H_review', lambda: review_carry_allocation(db, **csel))
    stage('carry_H_record', lambda: record_carry_allocation(db, operation_id='7' * 64, reviewed_digest=cr['reviewed_digest'], human_confirmed=True, **csel))
    call = dict(source_selections=sources, reason='Synthetic 2026 transition')
    tr = stage('transition_review', lambda: review_year_transition(db, **call))
    recorded = stage('transition_record', lambda: record_year_transition(db, operation_id='4' * 64, reviewed_digest=tr['reviewed_digest'], human_confirmed=True, **call))
    # Exact recorded retry adds neither checkpoint nor ledger entry.
    from sqlalchemy import select, func
    from app.models import LeaveBalanceTransaction, LeaveYearTransition
    before_count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    again = record_year_transition(db, operation_id='4'*64, reviewed_digest=tr['reviewed_digest'], human_confirmed=True, **call)
    assert again == recorded
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before_count
    assert all(b['source']['version']=='year2-recorded-v2' for b in recorded['balances'].values())
    assert recorded['ledger_delta']==recorded['new_opening_credit']=='0/1'
    w_source=recorded['balances']['W']['source']
    from fractions import Fraction
    expected_attribution = Fraction(9,7) if attributed else Fraction()
    assert Fraction(w_source['base']['source']['attributed_amount']) == expected_attribution
    from app.leave_recorded_year_view import read_verified_recorded_year_view
    for code,balance in recorded['balances'].items():
        bound=dict(source=balance['source'],source_digest=balance['source_digest'],source_year=2026,
                   carried_amount=balance['year_amount'],held_annual=balance['held_annual'])
        view=read_verified_recorded_year_view(db,result=bound,employee_id=emp,leave_type_id=kinds[code])
        assert view['provenance_ids'] <= {row[0] for row in balance['ledger']}
        if code in {'A','W'}:assert len(view['allocation_ids'])==1
        import copy
        from app.leave_month_earning import digest
        bad=copy.deepcopy(bound);bad['source']['net_amount']='123/1';bad['carried_amount']='123/1';bad['source_digest']=digest(bad['source'])
        with pytest.raises(ValueError):
            read_verified_recorded_year_view(db,result=bad,employee_id=emp,leave_type_id=kinds[code])
    # Later unrelated ledger growth cannot invalidate the named stored proof.
    from app.models import LeaveBalanceTransaction
    from decimal import Decimal
    growth_point=db.begin_nested()
    db.add(LeaveBalanceTransaction(employee_id=emp,leave_type_id=kinds['H'],transaction_type='adjustment',
        days_delta=Decimal('1'),earning_year=2028,released_on=date(2028,1,1),
        source_operation_key='synthetic:later-transition-growth',source_digest='a'*64,
        reason='Synthetic genuinely later history',created_by='synthetic'))
    db.flush()
    after_growth=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    assert record_year_transition(db,operation_id='4'*64,reviewed_digest=tr['reviewed_digest'],human_confirmed=True,**call)==recorded
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==after_growth
    # Fresh monthly admission still refuses unrelated history. Do not weaken it.
    growth_point.rollback()
    class Feb(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 2, 15, 9, 0, tzinfo=tz or CAIRO_TZ)
    for mod in ('app.leave_month_earning', 'app.leave_carry_source'):
        monkeypatch.setattr(mod + '.datetime', Feb)
    from app.models import LeaveRegisterEntry
    for d in range(1, 32):
        db.add(LeaveRegisterEntry(employee_id=emp, work_date=date(2027, 1, d), code='', source='synthetic', recorded_by='synthetic'))
    db.flush()
    from app.leave_month_earning import review_month_earning
    ident = {k: args[k] for k in IDENT}
    months = [date(2027, 1, 1)]
    mc = dict(selection_id=args['selection_id'], month_start=date(2027, 1, 1), evaluated_on=date(2027, 2, 1),
        current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **ident)
    rv2 = stage('jan2027_earning_review', lambda: review_month_earning(db, **mc))
    from app.leave_month_earning import apply_month_earning
    stage('jan2027_earning_apply', lambda: apply_month_earning(db, operation_id='3' * 64, reviewed_digest=rv2['reviewed_digest'],
        human_confirmed=True, reason='Synthetic Jan 2027 after real transition 2026', **mc))
    # Evidence the chain used the real post-allocation sources and the real (unpatched) transition admission.
    import json
    from sqlalchemy import select as sel_
    from app.models import LeaveYearTransition, LeaveMonthEarning
    assert trans_m.verify_transition_for_month.__module__ == 'app.leave_year_transition'   # no scaffold: original function
    row = db.scalar(sel_(LeaveYearTransition).where(LeaveYearTransition.employee_id == emp, LeaveYearTransition.source_year == 2026))
    result = json.loads(row.result_json)
    kinds_closing = {c: b['closing_receipt'][0] for c, b in result['balances'].items()}
    assert kinds_closing == {'A': 'annual', 'W': 'zero', 'H': 'carry'}
    a_allocations = result['balances']['A']['source']['allocations']
    w_allocations = result['balances']['W']['source']['allocations']
    assert [item['kind'] for item in a_allocations] == ['cash']
    assert [item['kind'] for item in w_allocations] == ['zero']
    assert db.get(LeaveMonthEarning, '3' * 64) is not None
