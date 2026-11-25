import json
from datetime import date, datetime
from fractions import Fraction
import pytest
from sqlalchemy import select, text
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement
from app.leave_carry_source import review_carry_source
from app.leave_cash_out import apply_cash_out, review_cash_out, digest, canonical
from app.models import CAIRO_TZ, LeaveAnnualSettlement, LeaveCashOut
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_cash_out import payroll

FULL = date(2025, 1, 1)


def settle(prior_year, carry_days=True):
    db, employee, kinds, args = prior_year
    run, item = payroll(db, employee, start=date(2025, 12, 1), end=date(2025, 12, 31), status='draft')
    sel = dict(source_selection={**args, 'leave_type_id': kinds['A']}, full_annual_entitlement=15, payroll_item_id=item.id,
               available_from=date(2026, 1, 1) if carry_days else None,
               available_through=date(2026, 4, 30) if carry_days else None, reason='Synthetic annual close')
    reviewed = review_annual_settlement_apply(db, **sel)
    apply_annual_settlement(db, operation_id='f' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **sel)
    auth = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    return db, employee, kinds, item, auth


def carry(db, employee, kinds, auth):
    return review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['A'], source_year=2025,
                               reviewed_on=date(2026, 1, 1), **auth)


@pytest.mark.parametrize('prior_year,remaining,carried', [
    ({'hire_date': FULL, 'used_annual': 0}, '15', '15/2'),
    ({'hire_date': FULL, 'used_annual': 6}, '9', '3/2'),
    ({'hire_date': FULL, 'used_annual': 9}, '6', '0'),
], indirect=['prior_year'])
def test_source_year_after_settlement_equals_carry_remainder_and_debit_is_in_year_2025(prior_year, remaining, carried):
    db, employee, kinds, item, auth = settle(prior_year, carry_days=carried != '0')
    out = carry(db, employee, kinds, auth)
    assert Fraction(str(out['amount'])) == Fraction(carried)
    zero = review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['A'], source_year=2026,
                               reviewed_on=datetime.now(CAIRO_TZ).date(), **auth)
    assert Fraction(str(zero['amount'])) == 0   # 2026 is not mis-attributed the 2025 debit


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 0}], indirect=True)
@pytest.mark.parametrize('mode', ['settlement_deleted', 'settlement_year', 'audit_actor', 'audit_reason', 'audit_deleted',
                                  'result_plan_cash', 'cash_source_year', 'cash_receipt_days', 'carry_rule'])
def test_forged_annual_or_cash_evidence_makes_carry_source_refuse(prior_year, mode):
    db, employee, kinds, item, auth = settle(prior_year)
    carry(db, employee, kinds, auth)
    result = json.loads(db.scalar(select(LeaveAnnualSettlement.result_json)))
    tables = ['leave_annual_settlements', 'leave_cash_outs', 'audit_logs', 'leave_balance_transactions']
    for t in tables: db.execute(text(f'ALTER TABLE {t} DISABLE TRIGGER USER'))
    if mode == 'settlement_deleted':
        db.execute(text('DELETE FROM leave_annual_settlements'))
    elif mode == 'settlement_year':
        db.execute(text('UPDATE leave_annual_settlements SET source_year=2024'))
    elif mode == 'audit_actor':
        db.execute(text("UPDATE audit_logs SET actor='someone' WHERE action='leave_annual_settled'"))
    elif mode == 'audit_reason':
        db.execute(text("UPDATE audit_logs SET reason='Other' WHERE action='leave_annual_settled'"))
    elif mode == 'audit_deleted':
        db.execute(text("DELETE FROM audit_logs WHERE action='leave_annual_settled'"))
    elif mode == 'result_plan_cash':
        forged = json.loads(json.dumps(result)); forged['plan']['annual']['cash_days'] = '1/1'
        db.execute(text('UPDATE leave_annual_settlements SET result_json=:r, result_digest=:d'), dict(r=canonical(forged), d=digest(forged)))
    elif mode == 'cash_source_year':
        src = json.loads(db.scalar(select(LeaveCashOut.source_json))); src['source_year'] = 2024
        db.execute(text('UPDATE leave_cash_outs SET source_json=:s'), dict(s=canonical(src)))
    elif mode == 'cash_receipt_days':
        db.execute(text("UPDATE leave_cash_outs SET days_canonical='1/1'"))
    else:
        forged = json.loads(json.dumps(result)); a = forged['plan']['annual']
        a['carried_days'], a['cash_days'] = '14/1', '1/1'
        db.execute(text('UPDATE leave_annual_settlements SET result_json=:r, result_digest=:d'), dict(r=canonical(forged), d=digest(forged)))
    for t in tables: db.execute(text(f'ALTER TABLE {t} ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError):
        carry(db, employee, kinds, auth)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 0}], indirect=True)
def test_manual_cash_out_cannot_be_passed_off_as_annual_source_year_and_blocks_annual(prior_year):
    db, employee, kinds, args = prior_year
    run, item = payroll(db, employee, start=date(2025, 12, 1), end=date(2025, 12, 31), status='draft')
    auth = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    manual = dict(employee_id=employee.id, leave_type_id=kinds['A'], days=Fraction(15, 2), payroll_item_id=item.id,
                  evaluated_on=date(2026, 1, 5), source_year=2025, **auth)   # caller asserts a source year with no annual receipt
    review = review_cash_out(db, **manual)
    apply_cash_out(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, reason='Synthetic', **manual)
    with pytest.raises(ValueError):
        carry(db, employee, kinds, auth)       # tagged 2025 but no annual settlement: refuses
    sel = dict(source_selection={**args, 'leave_type_id': kinds['A']}, full_annual_entitlement=15, payroll_item_id=item.id,
               available_from=date(2026, 1, 1), available_through=date(2026, 4, 30), reason='Synthetic')
    with pytest.raises(ValueError):
        review_annual_settlement_apply(db, **sel)   # and never pays the entitlement twice
