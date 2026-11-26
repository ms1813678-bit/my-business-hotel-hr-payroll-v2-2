from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, func, text
from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry, verify_expiry_receipt
from app.leave_cash_out import verified_released_balance
from app.models import LeaveBalanceTransaction, LeaveCarryExpiry, AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_carry_expiry import prepared_expiry
from tests.test_leave_next_year_earning import FULL

P = [{'hire_date': FULL, 'used_annual': 6}]


def posted(prior_year, category='W'):
    db, call, usage, review = prepared_expiry(prior_year, category)
    confirm = dict(operation_id='f' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    first = apply_carry_expiry(db, **confirm)
    auth = {k: call[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    return db, call, confirm, first, auth


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_expiry_date_boundary_no_early_or_future_exclusion(prior_year):
    db, call, usage, review = prepared_expiry(prior_year, 'W')
    for bad in (date(2026, 4, 30), date(2026, 4, 1), date(2099, 1, 1)):
        with pytest.raises(ValueError):
            review_carry_expiry(db, **{**call, 'evaluated_on': bad})
    assert db.scalar(select(func.count()).select_from(LeaveCarryExpiry)) == 0


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('forge', ['ledger_amount', 'ledger_reason', 'ledger_year', 'audit_actor', 'ledger_digest'])
def test_forged_expiry_posting_is_detected_by_every_balance_reader(prior_year, forge):
    db, call, confirm, first, auth = posted(prior_year)
    stmts = {'ledger_amount': ('leave_balance_transactions', "UPDATE leave_balance_transactions SET exact_numerator=exact_numerator-1 WHERE source_operation_key LIKE 'leave_carry_expiry:%' AND exact_numerator IS NOT NULL"),
             'ledger_reason': ('leave_balance_transactions', "UPDATE leave_balance_transactions SET reason='forged' WHERE source_operation_key LIKE 'leave_carry_expiry:%'"),
             'ledger_year': ('leave_balance_transactions', "UPDATE leave_balance_transactions SET earning_year=earning_year+1 WHERE source_operation_key LIKE 'leave_carry_expiry:%'"),
             'ledger_digest': ('leave_balance_transactions', "UPDATE leave_balance_transactions SET source_digest=repeat('0',64) WHERE source_operation_key LIKE 'leave_carry_expiry:%'"),
             'audit_actor': ('audit_logs', "UPDATE audit_logs SET actor='forger' WHERE action='leave_carry_expired'")}
    table, stmt = stmts[forge]
    db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
    n = db.execute(text(stmt)).rowcount
    db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
    db.expire_all()
    assert n >= 1
    with pytest.raises(ValueError):
        verified_released_balance(db, employee_id=call['employee_id'], leave_type_id=call['leave_type_id'], identity=auth)
    with pytest.raises(ValueError):
        apply_carry_expiry(db, **confirm)          # replay of a forged receipt must not be accepted


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_expiry_excludes_exactly_the_unconsumed_remainder_from_the_all_time_balance(prior_year):
    db, call, usage, review = prepared_expiry(prior_year, 'W')
    auth = {k: call[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    before = verified_released_balance(db, employee_id=call['employee_id'], leave_type_id=call['leave_type_id'], identity=auth)['balance']
    first = apply_carry_expiry(db, operation_id='f' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    after = verified_released_balance(db, employee_id=call['employee_id'], leave_type_id=call['leave_type_id'], identity=auth)['balance']
    excluded = Fraction(first['plan']['excluded'])
    assert before - after == excluded and excluded == Fraction(first['plan']['grant']['ceiling']) - Fraction(first['plan']['consumed'])
    assert excluded > 0 and Fraction(first['plan']['consumed']) == 3
