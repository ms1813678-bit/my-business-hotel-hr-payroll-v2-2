"""Actual source-year boundary tests, not authority from arbitrary ledger tags."""
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_carry_source import review_carry_source
from app.models import LeaveBalanceTransaction, LeaveType
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401


def test_monthly_exact_year_source_is_separate_from_alltime_opening(monthly):
    db, employee, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    identity = {k: args(date(2026, 2, 1), date(2026, 2, 28))[k]
                for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    result = review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['W'],
                               source_year=2026, reviewed_on=date(2026, 11, 25), **identity)
    assert result['amount'] == Fraction(4, 7)
    assert result['source']['selected_ids']
    assert not result['posting_allowed'] and not result['source_verified']
    # Year-tag availability alone is not a receipt/source-year completeness proof.
    assert result['unresolved'] == ('source_year_completeness_and_opening_attribution_not_verified',)


def test_active_custom_tracking_type_included_nontracking_counter_refused(history_context):
    db, actor, identity, fact = history_context
    custom = LeaveType(code='synthetic-custom-carry', name_ar='Synthetic', name_en='Synthetic',
                       tracks_balance=True, active=True, is_paid=True)
    counter = LeaveType(code='synthetic-counter', name_ar='Synthetic', name_en='Synthetic',
                        tracks_balance=False, active=True, is_paid=True)
    db.add_all([custom, counter]); db.flush()
    result = review_carry_source(db, employee_id=fact.employee_id, leave_type_id=custom.id,
                               source_year=2026, reviewed_on=date(2026, 11, 25), **identity)
    assert result['source']['leave_type_code'] == 'synthetic-custom-carry'
    with pytest.raises(ValueError, match='balance-tracking'):
        review_carry_source(db, employee_id=fact.employee_id, leave_type_id=counter.id,
                            source_year=2026, reviewed_on=date(2026, 11, 25), **identity)


def test_manual_unattributed_opening_is_not_current_year_earning(history_context):
    db, actor, identity, fact = history_context
    kind = db.scalar(select(LeaveType).where(LeaveType.code == 'annual'))
    db.add(LeaveBalanceTransaction(employee_id=fact.employee_id, leave_type_id=kind.id,
                                  transaction_type='opening', days_delta=Decimal(10),
                                  reason='Synthetic unattributed source', created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError, match='Unattributed opening'):
        review_carry_source(db, employee_id=fact.employee_id, leave_type_id=kind.id,
                            source_year=2026, reviewed_on=date(2026, 11, 25), **identity)


def test_arbitrary_year_tag_is_not_verified_monthly_source(monthly):
    db, employee, kinds, args = monthly
    auth = {k: args(date(2026, 2, 1), date(2026, 2, 28))[k]
            for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kinds['W'],
        transaction_type='adjustment', days_delta=Decimal(4), earning_year=2026,
        source_operation_key='synthetic:unverified', source_digest='f'*64, released_on=date(2026, 2, 28),
        reason='Synthetic unverified year tag', created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError, match='provenance'):
        review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['W'],
            source_year=2026, reviewed_on=date(2026, 11, 25), **auth)


def test_held_annual_is_separate_unreleased_source_not_spendable(monthly):
    db, employee, kinds, args = monthly
    selection = args(date(2026, 2, 1), date(2026, 2, 28))
    post(db, selection)
    auth = {k: selection[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    result = review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['A'],
        source_year=2026, reviewed_on=date(2026, 11, 25), **auth)
    assert result['amount'] == -1
    assert result['source']['held_annual'] == [('a'*64, '5/28')]
    assert not result['source_verified'] and not result['posting_allowed']


def test_future_review_date_cannot_imply_closed_year(monthly):
    db, employee, kinds, args = monthly
    auth = {k: args(date(2026, 2, 1), date(2026, 2, 28))[k]
            for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    with pytest.raises(ValueError, match='current review'):
        review_carry_source(db, employee_id=employee.id, leave_type_id=kinds['W'],
            source_year=2026, reviewed_on=date(2026, 12, 31), **auth)
