import json
from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, text
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement
from app.leave_cash_out import apply_cash_out, review_cash_out, digest, canonical
from app.leave_workflow import leave_balance
from app.models import LeaveAnnualSettlement, LeaveCashOut, AuditLog, CAIRO_TZ
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_cash_out import payroll

OPT = [{'hire_date': date(2025, 1, 1), 'used_annual': 0}]


def selection(prior_year, reviewed_on=None):
    db, employee, kinds, args = prior_year
    run, item = payroll(db, employee, start=date(2025, 12, 1), end=date(2025, 12, 31), status='draft')
    source = {**args, 'leave_type_id': kinds['A']}
    return dict(source_selection=source, full_annual_entitlement=15, payroll_item_id=item.id,
        available_from=date(2026, 1, 1), available_through=date(2026, 4, 30), reason='Synthetic annual close'), item


@pytest.mark.parametrize('prior_year', OPT, indirect=True)
def test_manual_cash_out_of_same_entitlement_then_annual_settlement_cannot_pay_more_than_half(prior_year):
    db, employee, kinds, args = prior_year
    sel, item = selection(prior_year)
    auth = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    manual = dict(employee_id=employee.id, leave_type_id=kinds['A'], days=Fraction(15, 2), payroll_item_id=item.id,
                  evaluated_on=date(2026, 1, 5), **auth)
    review = review_cash_out(db, **manual)
    apply_cash_out(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True,
                   reason='Synthetic manual cash-out', **manual)
    paid_before = db.scalar(select(__import__('sqlalchemy').func.count()).select_from(LeaveCashOut))
    try:
        reviewed = review_annual_settlement_apply(db, **sel)
        result = apply_annual_settlement(db, operation_id='f' * 64, reviewed_digest=reviewed['reviewed_digest'],
                                         human_confirmed=True, **sel)
    except ValueError as exc:
        print('DOUBLE_CASH_REFUSED', exc)
        return  # refusing is acceptable
    cash_total = sum(Fraction(json.loads(r.days_canonical)) if False else Fraction(r.days_canonical)
                     for r in db.scalars(select(LeaveCashOut)))
    carry_claim = Fraction(result['carry_days'])
    balance = Fraction(leave_balance(db, employee.id, kinds['A']))
    assert cash_total <= Fraction(15, 2), f'cash paid {cash_total} days of a 15 day entitlement (half cap 7.5)'
    assert balance == carry_claim, f'receipt claims carry {carry_claim} but ledger balance is {balance}'


@pytest.mark.parametrize('prior_year', OPT, indirect=True)
def test_annual_replay_rejects_consistently_forged_carry_even_with_matching_digest(prior_year):
    db, employee, kinds, args = prior_year
    sel, item = selection(prior_year)
    reviewed = review_annual_settlement_apply(db, **sel)
    confirm = dict(operation_id='f' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **sel)
    result = apply_annual_settlement(db, **confirm)
    row = db.execute(text('SELECT request_json FROM leave_annual_settlements')).one()
    forged = json.loads(json.dumps(result))
    forged['plan']['annual']['carried_days'] = '14/1'
    forged['plan']['annual']['cash_days'] = '1/1'
    forged['carry_days'] = '14/1'
    new_digest = digest(forged['plan'])
    request = json.loads(row.request_json); request['reviewed_digest'] = new_digest
    for t in ('leave_annual_settlements', 'audit_logs'):
        db.execute(text(f'ALTER TABLE {t} DISABLE TRIGGER USER'))
    db.execute(text('UPDATE leave_annual_settlements SET request_json=:q, request_digest=:qd, result_json=:r, result_digest=:rd, source_digest=:s'),
               dict(q=canonical(request), qd=digest(request), r=canonical(forged), rd=digest(forged), s=new_digest))
    db.execute(text("UPDATE audit_logs SET old_value=:s, new_value=:r WHERE action='leave_annual_settled'"),
               dict(s=new_digest, r=canonical(forged)))
    for t in ('leave_annual_settlements', 'audit_logs'):
        db.execute(text(f'ALTER TABLE {t} ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError):
        apply_annual_settlement(db, **{**confirm, 'reviewed_digest': new_digest})


@pytest.mark.parametrize('prior_year', OPT, indirect=True)
def test_next_year_carry_source_after_annual_settlement(prior_year):
    from datetime import datetime
    from app.leave_carry_source import review_carry_source
    db, employee, kinds, args = prior_year
    sel, item = selection(prior_year)
    reviewed = review_annual_settlement_apply(db, **sel)
    apply_annual_settlement(db, operation_id='f' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **sel)
    auth = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    try:
        out = review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['A'], source_year=2026,
                                  reviewed_on=datetime.now(CAIRO_TZ).date(), **auth)
        outcome = 'ok amount=%s' % out['amount']
    except ValueError as exc:
        outcome = str(exc)
    print('NEXT_YEAR_CARRY_SOURCE', outcome)
    assert outcome.startswith('ok'), outcome


@pytest.mark.parametrize('prior_year', OPT, indirect=True)
def test_verified_cash_out_debit_does_not_block_later_month_or_year_source_reviews(prior_year):
    from app.leave_completed_year_source import review_completed_year_source
    db, employee, kinds, args = prior_year
    sel, item = selection(prior_year)
    before = review_completed_year_source(db, **{**args,'leave_type_id':kinds['A']})
    reviewed = review_annual_settlement_apply(db, **sel)
    apply_annual_settlement(db, operation_id='f' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **sel)
    after = review_completed_year_source(db, **{**args,'leave_type_id':kinds['A']})  # same 2025 source must still verify
    assert after['source_verified'] and after['amount'] == Fraction(before['amount']) - Fraction(15,2)
