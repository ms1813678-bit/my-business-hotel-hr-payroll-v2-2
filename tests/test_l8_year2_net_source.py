# ruff: noqa: F811
# Imported pytest fixtures intentionally share test parameter names.
"""Real second-year net source: transitions, 12 monthly receipts, consumption attribution, add-back."""
from calendar import monthrange
from datetime import date, datetime, timedelta
from fractions import Fraction
import json
import pytest
from sqlalchemy import select, func
from app.models import (CAIRO_TZ, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveMonthEarning, LeaveRegisterEntry,
                        LeaveCarryConsumption)
from app.leave_month_earning import review_month_earning, apply_month_earning
from app.leave_carry_consumption import review_carry_consumption, record_carry_consumption
from app.leave_year2_net_source import review_year2_net_source
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_carry_consumption import ready, FULL

IDENT = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2027, 1, 15, 9, 0, tzinfo=tz or CAIRO_TZ)


def full_year(prior_year, monkeypatch):
    """Jan applied by ready(); consume 1 + 2/7 day of the 2025 W carry; apply Feb..Dec; return db, sel, args."""
    for mod in ('app.leave_month_earning', 'app.leave_carry_source', 'app.leave_completed_year_source', 'app.leave_year2_net_source',
                'app.leave_carry_consumption', 'app.leave_year_transition'):
        try:
            monkeypatch.setattr(mod + '.datetime', Clock)
        except AttributeError:
            pass
    db, selection = ready(prior_year)
    employee = prior_year[1]
    for extra in (dict(), dict(work_date=date(2026, 1, 10), quantity=Fraction(2, 7))):
        call = {**selection, **extra}
        review = review_carry_consumption(db, **call)
        record_carry_consumption(db, operation_id=('a' if not extra else 'c') * 64,
            reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    auth = {k: selection[k] for k in IDENT}
    sel_id = db.scalar(select(LeaveMonthEarning.selection_id).where(LeaveMonthEarning.employee_id == employee.id))
    for month in range(2, 13):
        for d in range(1, monthrange(2026, month)[1] + 1):
            db.add(LeaveRegisterEntry(employee_id=employee.id, work_date=date(2026, month, d),
                code='W' if d in (5, 12) else '', source='synthetic', recorded_by='synthetic'))
    db.flush()
    for month in range(2, 13):
        start = date(2026, month, 1)
        months = [date(2026, m, 1) for m in range(1, month + 1)]
        call = dict(selection_id=sel_id, month_start=start,
            evaluated_on=date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1),
            current_insurance_years={(employee.id, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **auth)
        review = review_month_earning(db, **call)
        apply_month_earning(db, operation_id=format(100 + month, '064x'), reviewed_digest=review['reviewed_digest'],
            human_confirmed=True, reason='Synthetic year 2 month', **call)
    months = [date(2026, m, 1) for m in range(1, 13)]
    args = dict(employee_id=employee.id, leave_type_id=prior_year[2]['W'], source_year=2026, selection_id=sel_id,
        reviewed_on=date(2027, 1, 2), current_insurance_years={(employee.id, m): None for m in months},
        age_evaluation_dates={m: m for m in months}, **auth)
    return db, args


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_real_year2_net_adds_back_only_attributed_usage_and_reconciles_to_grant(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    kind = args['leave_type_id']
    rows26 = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == args['employee_id'],
        LeaveBalanceTransaction.leave_type_id == kind, LeaveBalanceTransaction.earning_year == 2026)).all()
    from app.leave_exact_amount import ledger_amount, as_fraction
    expected_own = sum((as_fraction(ledger_amount(r)) for r in rows26), Fraction())
    result = review_year2_net_source(db, **args)
    plan = result['source']
    assert not result['posting_allowed'] and result['source_verified']
    assert Fraction(plan['own_rows_amount']) == expected_own
    assert Fraction(plan['attributed_to_prior_grants']) == Fraction(1) + Fraction(2, 7) == Fraction(9, 7)
    assert result['amount'] == expected_own + Fraction(9, 7)
    assert len(plan['covered_months']) == 12 and len(plan['transitions']) == 1
    # no second debit from the attribution: ledger rows are untouched by the review
    count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    assert review_year2_net_source(db, **args)['source_digest'] == result['source_digest']
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == count
    # reconciliation: remaining carried grant + year-2 net == all-time ledger balance (no expiry or zero here)
    grant = db.scalar(select(LeaveCarryAllocation).where(LeaveCarryAllocation.employee_id == args['employee_id'],
        LeaveCarryAllocation.leave_type_id == kind))
    ceiling = Fraction(json.loads(grant.result_json)['carried_amount'])
    consumed = sum((Fraction(c.quantity_canonical) for c in db.scalars(select(LeaveCarryConsumption)).all()), Fraction())
    assert (ceiling - consumed) + result['amount'] == Fraction(plan['all_time_balance'])
    # negative control: without the add-back the same oracle must fail
    assert (ceiling - consumed) + result['own_rows_amount'] != Fraction(plan['all_time_balance'])


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
@pytest.mark.parametrize('mode', ['opening_year', 'future', 'early', 'other_employee', 'tampered_receipt'])
def test_year2_net_refusals(prior_year, monkeypatch, mode):
    db, args = full_year(prior_year, monkeypatch)
    if mode == 'opening_year':
        args = {**args, 'source_year': 2025}
    if mode == 'future':
        args = {**args, 'reviewed_on': date(2027, 2, 1)}
    if mode == 'early':
        args = {**args, 'reviewed_on': date(2026, 12, 31)}
    if mode == 'other_employee':
        args = {**args, 'employee_id': args['employee_id'] + 1}
    if mode == 'tampered_receipt':
        day = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == args['employee_id'],
            LeaveRegisterEntry.work_date == date(2026, 3, 5)))
        day.code = ''
        db.flush()
    match = dict(opening_year='Opening-year', future='Actual completed', early='Actual completed',
        other_employee='belongs to another|Existing active|Employee', tampered_receipt='changed|Live earned|tier|source')[mode]
    with pytest.raises(ValueError, match=match):
        review_year2_net_source(db, **args)


# ---- additional full flows: negative control, omitted month, zero old grant, negative year net ----
from app.leave_year_transition import review_year_transition, record_year_transition
from app.leave_carry_allocation_store import review_carry_allocation, record_carry_allocation
from app.leave_zero_decision import review_zero_decision, record_zero_decision
from tests.test_l8_source_year_cash_binding import settle


def patch_clock(monkeypatch):
    for mod in ('app.leave_month_earning', 'app.leave_carry_source', 'app.leave_completed_year_source',
                'app.leave_year2_net_source', 'app.leave_carry_consumption', 'app.leave_year_transition'):
        try:
            monkeypatch.setattr(mod + '.datetime', Clock)
        except AttributeError:
            pass


def zero_year(prior_year, monkeypatch, *, skip_dec=False, w_days=(5, 12), jan_w_days=(3, 10, 17)):
    """2025 closes with W zeroed (H and A as in the transition fixtures); then 2026 months are applied."""
    patch_clock(monkeypatch)
    db, employee, kinds, item, auth = settle(prior_year)
    args = prior_year[3]
    sources = {c: {**args, 'leave_type_id': kinds[c]} for c in kinds}
    zr = review_zero_decision(db, source_selection=sources['W'], reason='Synthetic zero W')
    record_zero_decision(db, operation_id='1' * 64, reviewed_digest=zr['reviewed_digest'], human_confirmed=True,
        source_selection=sources['W'], reason='Synthetic zero W')
    sel = dict(source_selection=sources['H'], available_from=None, available_through=None, reason='Synthetic closing')
    rv = review_carry_allocation(db, **sel)
    record_carry_allocation(db, operation_id='2' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, **sel)
    call = dict(source_selections=sources, reason='Synthetic')
    review = review_year_transition(db, **call)
    record_year_transition(db, operation_id='d' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    emp = employee.id
    day = date(2026, 1, 1)
    while day <= date(2026, 12, 31):
        days = jan_w_days if day.month == 1 else w_days
        db.add(LeaveRegisterEntry(employee_id=emp, work_date=day, code='W' if day.day in days else '', source='synthetic', recorded_by='synthetic'))
        day += timedelta(days=1)
    db.flush()
    ident = {k: sources['A'][k] for k in IDENT}
    sel_id = sources['A']['selection_id']
    last = 11 if skip_dec else 12
    for month in range(1, last + 1):
        months = [date(2026, m, 1) for m in range(1, month + 1)]
        mc = dict(selection_id=sel_id, month_start=date(2026, month, 1),
            evaluated_on=date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1),
            current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **ident)
        rv = review_month_earning(db, **mc)
        apply_month_earning(db, operation_id=format(200 + month, '064x'), reviewed_digest=rv['reviewed_digest'],
            human_confirmed=True, reason='Synthetic year 2 month', **mc)
    months = [date(2026, m, 1) for m in range(1, 13)]
    return db, dict(employee_id=emp, leave_type_id=kinds['W'], source_year=2026, selection_id=sel_id,
        reviewed_on=date(2027, 1, 2), current_insurance_years={(emp, m): None for m in months},
        age_evaluation_dates={m: m for m in months}, **ident)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_net_after_zeroed_old_grant_has_no_addback_and_reconciles(prior_year, monkeypatch):
    db, args = zero_year(prior_year, monkeypatch)
    result = review_year2_net_source(db, **args)
    plan = result['source']
    assert Fraction(plan['attributed_to_prior_grants']) == 0 and plan['attributions'] == []
    # the zero debit is tagged 2025, so it is not in 2026; all-time balance is exactly the 2026 net
    assert result['amount'] == Fraction(plan['all_time_balance']) == Fraction(plan['own_rows_amount'])


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_negative_net_is_reported_exactly_never_floored(prior_year, monkeypatch):
    db, args = zero_year(prior_year, monkeypatch, jan_w_days=tuple(range(1, 31)), w_days=tuple(range(1, 31)))
    result = review_year2_net_source(db, **args)
    assert result['amount'] < 0 and Fraction(result['source']['net_amount']) == result['amount']
    assert result['amount'] == Fraction(result['source']['all_time_balance'])


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_omitted_december_receipt_refuses_from_fresh_fixture(prior_year, monkeypatch):
    db, args = zero_year(prior_year, monkeypatch, skip_dec=True)
    with pytest.raises(ValueError, match='omit or exceed'):
        review_year2_net_source(db, **args)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_net_refuses_when_consumed_register_day_changes_after_attribution(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    assert review_year2_net_source(db, **args)['source_verified']
    day = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == args['employee_id'],
        LeaveRegisterEntry.work_date == date(2026, 1, 3)))
    day.code = ''
    db.flush()
    with pytest.raises(ValueError):
        review_year2_net_source(db, **args)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_net_refuses_over_cap_history_bound(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    # the bound is on fetched history: a 10001-row fetch must refuse before any binding is counted
    class Rows(list):
        def all(self):
            return self
    original = db.scalars
    def scalars(stmt, *a, **k):
        out = original(stmt, *a, **k)
        if 'leave_carry_consumptions' in str(stmt):
            return Rows([None] * 10001)
        return out
    monkeypatch.setattr(db, 'scalars', scalars)
    with pytest.raises(ValueError, match='bounded review scope'):
        review_year2_net_source(db, **args)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_probe_which_check_catches_consumed_day_change(prior_year, monkeypatch):
    import app.leave_year2_net_source as module
    """Diagnostic: with the new usage/grant replay disabled, do the older checks still refuse?"""
    db, args = full_year(prior_year, monkeypatch)
    monkeypatch.setattr(module, '_usage', lambda db, **k: json.loads(db.scalar(select(LeaveCarryConsumption.result_json).where(
        LeaveCarryConsumption.usage_ledger_id == k['usage_ledger_id'], LeaveCarryConsumption.work_date == k['work_date'])))['plan']['usage'])
    monkeypatch.setattr(module, '_grant', lambda db, **k: json.loads(db.scalar(select(LeaveCarryConsumption.result_json).where(
        LeaveCarryConsumption.grant_id == k['grant_id'], LeaveCarryConsumption.work_date == k['work_date'])))['plan']['grant'])
    day = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == args['employee_id'],
        LeaveRegisterEntry.work_date == date(2026, 1, 3)))
    day.code = ''
    db.flush()
    try:
        review_year2_net_source(db, **args)
        outcome = 'NOT_REFUSED_by_older_checks'
    except ValueError as exc:
        outcome = 'refused: ' + str(exc)
    assert outcome.startswith('refused: Prior earned source changed'), outcome


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_net_after_expired_old_grant_keeps_attribution_and_excludes_expiry_debit(prior_year, monkeypatch):
    """Old W carry fully attributed, then expired by explicit HR exclusion inside the year; then the net of 2026."""
    from tests.test_leave_carry_expiry import prepared_expiry
    from app.leave_carry_expiry import apply_carry_expiry
    from app.models import LeaveCarryExpiry
    from app.leave_exact_amount import ledger_amount, as_fraction
    db, call, usage, review = prepared_expiry(prior_year, 'W')
    patch_clock(monkeypatch)
    apply_carry_expiry(db, operation_id='f' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    emp = usage['employee_id']
    auth = {k: usage[k] for k in IDENT}
    sel_id = db.scalar(select(LeaveMonthEarning.selection_id).where(LeaveMonthEarning.employee_id == emp))
    for month in range(5, 13):
        day = date(2026, month, 1)
        while day.month == month:
            db.add(LeaveRegisterEntry(employee_id=emp, work_date=day, code='', source='synthetic', recorded_by='synthetic'))
            day += timedelta(days=1)
        db.flush()
        months = [date(2026, m, 1) for m in range(1, month + 1)]
        mc = dict(selection_id=sel_id, month_start=date(2026, month, 1),
            evaluated_on=date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1),
            current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **auth)
        rv = review_month_earning(db, **mc)
        apply_month_earning(db, operation_id=format(300 + month, '064x'), reviewed_digest=rv['reviewed_digest'],
            human_confirmed=True, reason='Synthetic year 2 month', **mc)
    months = [date(2026, m, 1) for m in range(1, 13)]
    args = dict(employee_id=emp, leave_type_id=usage['leave_type_id'], source_year=2026, selection_id=sel_id,
        reviewed_on=date(2027, 1, 2), current_insurance_years={(emp, m): None for m in months},
        age_evaluation_dates={m: m for m in months}, **auth)
    result = review_year2_net_source(db, **args)
    plan = result['source']
    expiry = db.scalar(select(LeaveCarryExpiry))
    expiry_debit = db.get(LeaveBalanceTransaction, expiry.ledger_id)
    assert expiry_debit.earning_year == 2025                       # tagged to the grant year, not 2026
    assert Fraction(plan['attributed_to_prior_grants']) == 3       # Jan 3/10/17 attributed to the 2025 grant
    rows26 = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == emp,
        LeaveBalanceTransaction.leave_type_id == usage['leave_type_id'], LeaveBalanceTransaction.earning_year == 2026)).all()
    own = sum((as_fraction(ledger_amount(r)) for r in rows26), Fraction())
    assert Fraction(plan['own_rows_amount']) == own and result['amount'] == own + 3
    # the old grant is fully consumed or excluded, so the net of 2026 is the whole all-time balance
    assert result['amount'] == Fraction(plan['all_time_balance'])


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_net_annual_category_with_annual_grant_attribution(prior_year, monkeypatch):
    """Bounded annual case: usage attributed to the 2025 annual settlement grant; held annual stays outside the net."""
    from app.models import LeaveAnnualSettlement
    from app.leave_exact_amount import ledger_amount, as_fraction
    patch_clock(monkeypatch)
    db, selection = ready(prior_year, category='A')
    review = review_carry_consumption(db, **selection)
    record_carry_consumption(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    emp = prior_year[1].id
    auth = {k: selection[k] for k in IDENT}
    sel_id = db.scalar(select(LeaveMonthEarning.selection_id).where(LeaveMonthEarning.employee_id == emp))
    for month in range(2, 13):
        day = date(2026, month, 1)
        while day.month == month:
            db.add(LeaveRegisterEntry(employee_id=emp, work_date=day, code='', source='synthetic', recorded_by='synthetic'))
            day += timedelta(days=1)
    db.flush()
    for month in range(2, 13):
        months = [date(2026, m, 1) for m in range(1, month + 1)]
        mc = dict(selection_id=sel_id, month_start=date(2026, month, 1),
            evaluated_on=date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1),
            current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **auth)
        rv = review_month_earning(db, **mc)
        apply_month_earning(db, operation_id=format(400 + month, '064x'), reviewed_digest=rv['reviewed_digest'],
            human_confirmed=True, reason='Synthetic year 2 month', **mc)
    months = [date(2026, m, 1) for m in range(1, 13)]
    kind = prior_year[2]['A']
    args = dict(employee_id=emp, leave_type_id=kind, source_year=2026, selection_id=sel_id, reviewed_on=date(2027, 1, 2),
        current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **auth)
    result = review_year2_net_source(db, **args)
    plan = result['source']
    assert Fraction(plan['attributed_to_prior_grants']) == 1
    rows26 = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == emp,
        LeaveBalanceTransaction.leave_type_id == kind, LeaveBalanceTransaction.earning_year == 2026)).all()
    own = sum((as_fraction(ledger_amount(r)) for r in rows26), Fraction())
    assert Fraction(plan['own_rows_amount']) == own and result['amount'] == own + 1
    grant = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.employee_id == emp))
    carried = Fraction(json.loads(grant.result_json)['plan']['annual']['carried_days'])
    assert (carried - 1) + result['amount'] == Fraction(plan['all_time_balance'])
    assert plan['semantic'] == 'read_only_source_not_available_balance' and Fraction(plan['held_annual_not_in_net']) == 0


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
@pytest.mark.parametrize('service', ['zero', 'carry', 'transition'])
def test_year2_net_source_is_not_accepted_by_year_closing_services(prior_year, monkeypatch, service):
    """Boundary: annual has its own qualified consumer; zero/carry/transition stay guarded."""
    db, args = zero_year(prior_year, monkeypatch)
    assert review_year2_net_source(db, **args)['source_verified']          # the read-only source itself is fine
    kinds = prior_year[2]
    base = {k: v for k, v in args.items() if k != 'leave_type_id'}
    sel = lambda code: {**base, 'leave_type_id': kinds[code]}               # noqa: E731
    count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    message = 'separately reviewed year transition'
    with pytest.raises(ValueError, match=message):
        if service == 'zero':
            review_zero_decision(db, source_selection=sel('W'), reason='Synthetic year 2')
        if service == 'carry':
            review_carry_allocation(db, source_selection=sel('H'), available_from=date(2027, 1, 1),
                available_through=date(2027, 12, 31), reason='Synthetic year 2')
        if service == 'transition':
            review_year_transition(db, source_selections={c: sel(c) for c in kinds}, reason='Synthetic year 2')
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == count
