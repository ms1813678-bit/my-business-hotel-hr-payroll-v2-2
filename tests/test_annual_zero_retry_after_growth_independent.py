# ruff: noqa: F811
"""Independent reviewer tests (not author-shipped): annual-settlement and zero-decision
honest retries AFTER later legitimate ledger growth, plus annual actor-drift retry refusal.
Both replay branches are receipt-first by construction; these prove it with actual runs."""
from datetime import date
import pytest
from sqlalchemy import select, func
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement
from app.models import (LeaveAnnualSettlement, LeaveCashOut,
                        LeaveBalanceTransaction, AdminAccount, AdminSession)
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_leave_cash_out import payroll, counts

ANNUAL = [{'hire_date': date(2025, 1, 1), 'used_annual': 6}]


def growth(db, employee, type_id):
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=type_id,
        transaction_type='adjustment', days_delta=1, reason='Synthetic later adjustment',
        created_by='synthetic'))
    db.flush()


def annual_setup(prior_year):
    db, employee, kinds, args = prior_year
    _run, item = payroll(db, employee, start=date(2025, 12, 1), end=date(2025, 12, 31), status='draft')
    selection = dict(source_selection={**args, 'leave_type_id': kinds['A']}, full_annual_entitlement=15,
        payroll_item_id=item.id, available_from=date(2026, 1, 1), available_through=date(2026, 4, 30),
        reason='Synthetic actual annual close')
    reviewed = review_annual_settlement_apply(db, **selection)
    confirm = dict(operation_id='f' * 64, reviewed_digest=reviewed['reviewed_digest'],
                   human_confirmed=True, **selection)
    return db, employee, kinds, args, confirm


@pytest.mark.parametrize('prior_year', ANNUAL, indirect=True)
def test_annual_retry_after_later_legitimate_growth(prior_year):
    db, employee, kinds, args, confirm = annual_setup(prior_year)
    first = apply_annual_settlement(db, **confirm)
    growth(db, employee, kinds['A'])                       # same source category, unrelated new row
    from app.leave_annual_settlement_source import review_annual_settlement
    with pytest.raises(ValueError):
        review_annual_settlement(db, source_selection=confirm['source_selection'], full_annual_entitlement=15, other_half_action='carry')
    before = counts(db)
    assert apply_annual_settlement(db, **confirm) == first  # stored receipt, no refusal
    assert counts(db) == before
    db.add(LeaveBalanceTransaction(employee_id=employee.id,leave_type_id=kinds['A'],
        transaction_type='adjustment',days_delta=1,earning_year=2025,released_on=date(2025,12,31),
        source_operation_key='synthetic_backdated_legacy_annual_growth',source_digest='b'*64,
        reason='Synthetic later backdated provenance',created_by='synthetic'))
    db.flush()
    before=counts(db)
    assert apply_annual_settlement(db,**confirm)==first and counts(db)==before
    with pytest.raises(ValueError,match='unrelated ledger'):
        review_annual_settlement(db,source_selection=confirm['source_selection'],full_annual_entitlement=15,other_half_action='carry')
    assert db.scalar(select(func.count()).select_from(LeaveAnnualSettlement)) == 1
    assert db.scalar(select(func.count()).select_from(LeaveCashOut)) == 1


@pytest.mark.parametrize('prior_year', ANNUAL, indirect=True)
def test_annual_actor_drift_retry_refuses(prior_year):
    db, employee, kinds, args, confirm = annual_setup(prior_year)
    apply_annual_settlement(db, **confirm)
    acc2 = AdminAccount(username='synthetic-second-hr', password_hash='not-a-real-login',
                        role='system_admin', active=True)
    db.add(acc2); db.flush()
    sess2 = AdminSession(id='synthetic-second-session-a1', admin_account_id=acc2.id)
    db.add(sess2); db.flush()
    drifted = {**args, 'actor_id': acc2.id, 'session_id': sess2.id,
               'session_version': acc2.session_version}
    with pytest.raises(ValueError):
        apply_annual_settlement(db, **{**confirm, 'source_selection': {**confirm['source_selection'], **drifted}})
    assert db.scalar(select(func.count()).select_from(LeaveAnnualSettlement)) == 1




@pytest.mark.parametrize('prior_year', ANNUAL, indirect=True)
def test_annual_same_actor_other_valid_session_retry_refuses(prior_year):
    db, employee, kinds, args, confirm = annual_setup(prior_year)
    apply_annual_settlement(db, **confirm)
    sid='synthetic-same-actor-alternate-session'
    db.add(AdminSession(id=sid,admin_account_id=args['actor_id']));db.flush()
    before=counts(db)
    with pytest.raises(ValueError,match='recorded operation evidence'):
        apply_annual_settlement(db,**{**confirm,'source_selection':{**confirm['source_selection'],'session_id':sid}})
    assert counts(db)==before
