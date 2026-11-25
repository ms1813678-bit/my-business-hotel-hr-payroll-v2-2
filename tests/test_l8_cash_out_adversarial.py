import json
from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, text
from app.leave_cash_out import apply_cash_out, review_cash_out, digest, canonical
from app.models import LeaveBalanceTransaction, LeaveCashOut, AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_month_earning import monthly, post  # noqa
from tests.test_leave_cash_out import payroll, auth_of, cash, REASON


def setup(monthly, days='2/7'):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    _run, item = payroll(db, e)
    auth = auth_of(args)
    review, result = cash(db, auth, e, kinds['W'], item, days)
    call = dict(employee_id=e.id, leave_type_id=kinds['W'], days=days, payroll_item_id=item.id,
                evaluated_on=date(2026, 4, 2), **auth)
    return db, e, kinds, item, auth, review, result, call


def replay(db, review, call):
    return apply_cash_out(db, operation_id='d' * 64, reviewed_digest=review['reviewed_digest'],
                          human_confirmed=True, reason=REASON, **call)


def dba(db, *tables):
    for t in tables:
        db.execute(text(f'ALTER TABLE {t} DISABLE TRIGGER USER'))


def undba(db, *tables):
    for t in tables:
        db.execute(text(f'ALTER TABLE {t} ENABLE TRIGGER USER'))
    db.expire_all()


def test_replay_with_changed_days_item_reason_or_digest_is_refused(monthly):
    db, e, kinds, item, auth, review, result, call = setup(monthly)
    for change in (dict(days='1/7'), dict(evaluated_on=date(2026, 4, 3))):
        with pytest.raises(ValueError):
            replay(db, review, {**call, **change})
    with pytest.raises(ValueError):
        apply_cash_out(db, operation_id='d' * 64, reviewed_digest='0' * 64, human_confirmed=True, reason=REASON, **call)
    with pytest.raises(ValueError):
        apply_cash_out(db, operation_id='d' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True,
                       reason='Different reason', **call)


def test_equivalent_exact_day_spellings_bind_to_same_request(monthly):
    from decimal import Decimal
    db, e, kinds, item, auth, review, result, call = setup(monthly, days='1/2')
    for days in ('1/2', Fraction(1, 2), Decimal('0.5'), '0.50'):
        assert replay(db, review, {**call, 'days': days}) == result


def test_forged_consistent_ledger_amount_disagreeing_with_bound_source_days_is_refused(monthly):
    """DBA edits the ledger debit, result_json, result digest and audit consistently to a larger debit."""
    db, e, kinds, item, auth, review, result, call = setup(monthly)
    row = db.scalar(select(LeaveCashOut))
    ledger = db.get(LeaveBalanceTransaction, row.ledger_id)
    forged = json.loads(row.result_json)
    forged['ledger'][0]['amount'] = '-3/7'
    dba(db, 'leave_cash_outs', 'leave_balance_transactions', 'audit_logs')
    db.execute(text('UPDATE leave_balance_transactions SET exact_numerator=-3, exact_denominator=7 WHERE id=:i'), {'i': ledger.id})
    db.execute(text('UPDATE leave_cash_outs SET result_json=:r, result_digest=:d'), {'r': canonical(forged), 'd': digest(forged)})
    db.execute(text("UPDATE audit_logs SET new_value=:r WHERE action='leave_cash_out_applied'"), {'r': canonical(forged)})
    undba(db, 'leave_cash_outs', 'leave_balance_transactions', 'audit_logs')
    with pytest.raises(ValueError):
        replay(db, review, call)


@pytest.mark.parametrize('sql', [
    "UPDATE leave_cash_outs SET evaluated_on='2026-01-01'",
    "UPDATE leave_balance_transactions SET earning_year=1999 WHERE source_operation_key LIKE 'leave_cashout:%'",
    "UPDATE leave_balance_transactions SET released_on='1999-01-01' WHERE source_operation_key LIKE 'leave_cashout:%'",
    "UPDATE leave_balance_transactions SET created_by='someone-else' WHERE source_operation_key LIKE 'leave_cashout:%'",
])
def test_partial_forgery_of_receipt_or_ledger_identity_is_refused(monthly, sql):
    db, e, kinds, item, auth, review, result, call = setup(monthly)
    dba(db, 'leave_cash_outs', 'leave_balance_transactions')
    db.execute(text(sql))
    undba(db, 'leave_cash_outs', 'leave_balance_transactions')
    with pytest.raises(ValueError):
        replay(db, review, call)


def test_month_earning_ledger_identity_is_verified_as_strictly_as_carry_source(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    _run, item = payroll(db, e)
    auth = auth_of(args)
    call = dict(employee_id=e.id, leave_type_id=kinds['W'], days='1/7', payroll_item_id=item.id,
                evaluated_on=date(2026, 4, 2), **auth)
    review_cash_out(db, **call)
    dba(db, 'leave_balance_transactions')
    db.execute(text("UPDATE leave_balance_transactions SET earning_year=1999, released_on='1999-01-01' "
                    "WHERE source_operation_key LIKE 'month_earning:%'"))
    undba(db, 'leave_balance_transactions')
    with pytest.raises(ValueError):
        review_cash_out(db, **call)


def test_cash_debit_tagged_with_evaluation_year_is_not_silently_counted_by_carry_source(monthly):
    """Integration probe: the carry source for the evaluation year must refuse (or account for) a cash debit."""
    from app.leave_carry_source import review_carry_source
    db, e, kinds, item, auth, review, result, call = setup(monthly)
    today = __import__('datetime').datetime.now(__import__('app.models', fromlist=['CAIRO_TZ']).CAIRO_TZ).date()
    outcome = None
    try:
        outcome = review_carry_source(db, employee_id=e.id, leave_type_id=kinds['W'], source_year=2026,
                                      reviewed_on=today, **auth)
    except ValueError as exc:
        outcome = str(exc)
    print('CARRY_SOURCE_OUTCOME', outcome if isinstance(outcome, str) else outcome['amount'])
    assert isinstance(outcome, str) and 'provenance' in outcome
