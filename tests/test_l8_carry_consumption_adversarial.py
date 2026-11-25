import json
from datetime import date, timedelta
from fractions import Fraction
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from app.models import (AuditLog, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveCarryConsumption,
    LeaveAnnualSettlement, LeaveMonthEarning, LeaveRegisterEntry)
from app.leave_month_earning import review_month_earning, apply_month_earning
from app.leave_carry_consumption import review_carry_consumption, record_carry_consumption
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_next_year_earning import next_month, FULL

P = [{'hire_date': FULL, 'used_annual': 6}]


def ready(prior_year, code='W', days=(3, 10, 17), kind='carry'):
    db, call = next_month(prior_year)
    employee = prior_year[1]
    for r in db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == employee.id,
            LeaveRegisterEntry.work_date >= date(2026, 1, 1))).all():
        r.code = code if r.work_date.day in days else ''
    db.flush()
    reviewed = review_month_earning(db, **call)
    apply_month_earning(db, operation_id='b' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True,
        reason='Synthetic', **call)
    leave = prior_year[2][code]
    month = db.get(LeaveMonthEarning, 'b' * 64)
    usage = next(i for i in json.loads(month.result_json)['ledger'] if i['category'] == code and i['kind'] == 'usage')
    if kind == 'carry':
        grant = db.scalar(select(LeaveCarryAllocation).where(LeaveCarryAllocation.employee_id == employee.id,
            LeaveCarryAllocation.leave_type_id == leave)).id
    else:
        grant = db.scalar(select(LeaveAnnualSettlement.id).where(LeaveAnnualSettlement.employee_id == employee.id))
    auth = {k: call[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    base = dict(grant_kind=kind, grant_id=grant, employee_id=employee.id, leave_type_id=leave, monthly_receipt_id=month.id,
        usage_ledger_id=usage['id'], quantity=Fraction(1), reason='Synthetic selected grant', **auth)
    return db, base


def rec(db, base, day, qty, op):
    sel = {**base, 'work_date': date(2026, 1, day), 'quantity': Fraction(qty)}
    r = review_carry_consumption(db, **sel)
    return sel, record_carry_consumption(db, operation_id=op * 64, reviewed_digest=r['reviewed_digest'],
        human_confirmed=True, **sel)


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_fractional_attribution_accumulates_exactly_below_the_grant_ceiling(prior_year):
    db, base = ready(prior_year, days=(1, 2, 3, 4, 5, 6))
    for i, d in enumerate((1, 2, 3, 4, 5)):
        rec(db, base, d, 1, str(i + 1))
    rec(db, base, 6, '2/7', '6')                       # 5 + 2/7 = 37/7, well under the 365/7 fixture grant
    x = review_carry_consumption(db, **{**base, 'work_date': date(2026, 1, 6), 'quantity': Fraction(1, 7)})
    assert x['plan']['grant_attributed_after'] == '38/7' and x['plan']['grant']['ceiling'] == '365/7'


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_usage_row_amount_caps_total_attribution(prior_year):
    db, base = ready(prior_year, days=(1, 2))            # usage row -2
    rec(db, base, 1, 1, '1'); rec(db, base, 2, 1, '2')
    with pytest.raises(ValueError):                       # day 3 is not a W day, and the row is spent
        review_carry_consumption(db, **{**base, 'work_date': date(2026, 1, 3)})


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_annual_grant_branch_attributes_exactly_the_1_5_carry(prior_year):
    db, base = ready(prior_year, code='A', days=(5, 6, 7), kind='annual')
    rec(db, base, 5, 1, '1')
    rec(db, base, 6, '1/2', '2')                          # total 3/2 = annual carry ceiling
    with pytest.raises(ValueError, match='exceeds the remaining grant ceiling'):
        review_carry_consumption(db, **{**base, 'work_date': date(2026, 1, 7), 'quantity': Fraction(1, 7)})


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('sql', ['UPDATE leave_carry_consumptions SET quantity_canonical=quantity_canonical',
                                 'DELETE FROM leave_carry_consumptions', 'TRUNCATE leave_carry_consumptions'])
def test_receipt_rows_are_immutable(prior_year, sql):
    db, base = ready(prior_year)
    rec(db, base, 3, 1, '1')
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            db.execute(text(sql))


def off(db, table, stmt):
    db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
    db.execute(text(stmt))
    db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
    db.expire_all()


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('mode', ['audit_deleted', 'audit_reason', 'result_json', 'request_json', 'quantity_col',
                                  'register_code', 'usage_amount', 'grant_receipt_deleted'])
def test_forged_earlier_attribution_or_evidence_blocks_next_attribution_and_retry(prior_year, mode):
    db, base = ready(prior_year)
    sel, first = rec(db, base, 3, 1, '1')
    a = "action='leave_carry_consumed'"
    if mode == 'audit_deleted': off(db, 'audit_logs', f'DELETE FROM audit_logs WHERE {a}')
    elif mode == 'audit_reason': off(db, 'audit_logs', f"UPDATE audit_logs SET reason='x' WHERE {a}")
    elif mode == 'result_json': off(db, 'leave_carry_consumptions', "UPDATE leave_carry_consumptions SET result_json=replace(result_json,'1/1','1/2')")
    elif mode == 'request_json': off(db, 'leave_carry_consumptions', "UPDATE leave_carry_consumptions SET request_json=replace(request_json,'Synthetic','Changed')")
    elif mode == 'quantity_col': off(db, 'leave_carry_consumptions', "UPDATE leave_carry_consumptions SET quantity_canonical='1/2'")
    elif mode == 'register_code':
        r = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == base['employee_id'],
            LeaveRegisterEntry.work_date == date(2026, 1, 3))); r.code = 'U'; db.flush()
    elif mode == 'usage_amount':
        off(db, 'leave_balance_transactions', f"UPDATE leave_balance_transactions SET exact_numerator=NULL, exact_denominator=NULL, days_delta=-9 WHERE id={base['usage_ledger_id']}")
    else:
        off(db, 'leave_carry_allocations', 'DELETE FROM leave_carry_allocations WHERE id=:g'.replace(':g', f"'{base['grant_id']}'"))
    with pytest.raises(Exception):
        review_carry_consumption(db, **{**sel, 'work_date': date(2026, 1, 10), 'quantity': Fraction(1)})
    with pytest.raises(Exception):
        record_carry_consumption(db, operation_id='1' * 64, reviewed_digest=first['plan'] and
            json.loads(db.scalar(select(LeaveCarryConsumption.request_json)))['reviewed_digest'] if mode not in ('request_json',) else '0' * 64,
            human_confirmed=True, **sel)


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('mode', ['quantity', 'reason', 'day', 'grant'])
def test_retry_with_changed_selection_refuses(prior_year, mode):
    db, base = ready(prior_year)
    sel, first = rec(db, base, 3, 1, '1')
    digest_ = json.loads(db.scalar(select(LeaveCarryConsumption.request_json)))['reviewed_digest']
    changed = {**sel}
    if mode == 'quantity': changed['quantity'] = Fraction(1, 2)
    if mode == 'reason': changed['reason'] = 'Other'
    if mode == 'day': changed['work_date'] = date(2026, 1, 10)
    if mode == 'grant': changed['grant_id'] = 'f' * 64
    with pytest.raises(ValueError):
        record_carry_consumption(db, operation_id='1' * 64, reviewed_digest=digest_, human_confirmed=True, **changed)


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_audit_failure_rolls_back_the_attribution(prior_year, monkeypatch):
    db, base = ready(prior_year)
    sel = {**base, 'work_date': date(2026, 1, 3)}
    r = review_carry_consumption(db, **sel)
    original = db.flush
    def fail(*a, **k):
        if any(isinstance(x, AuditLog) and x.action == 'leave_carry_consumed' for x in db.new):
            raise RuntimeError('Synthetic audit failure')
        return original(*a, **k)
    monkeypatch.setattr(db, 'flush', fail)
    with pytest.raises(RuntimeError):
        record_carry_consumption(db, operation_id='1' * 64, reviewed_digest=r['reviewed_digest'], human_confirmed=True, **sel)
    monkeypatch.undo()
    assert db.scalar(select(func.count()).select_from(LeaveCarryConsumption)) == 0
