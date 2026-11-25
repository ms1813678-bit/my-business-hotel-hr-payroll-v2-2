"""Release-only guard coverage, synthetic real PostgreSQL fixtures."""
from datetime import date

import pytest
from sqlalchemy import event, func, select

from app.leave_month_earning import apply_annual_release, review_annual_release
from app.models import AuditLog, LeaveBalanceTransaction, LeaveMonthEarning
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401


def held(monthly):
    db, employee, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    post(db, args(date(2026, 3, 1), date(2026, 3, 31)), 'b' * 64)
    return db, args(date(2026, 3, 1), date(2026, 4, 1))


def snapshot(db):
    return tuple(db.scalar(select(func.count()).select_from(model)) for model in
                 (LeaveMonthEarning, LeaveBalanceTransaction, AuditLog))


def test_release_requires_elapsed_full_month_and_posted_source(monthly):
    db, employee, kinds, args = monthly
    before = snapshot(db)
    with pytest.raises(ValueError, match='Full first calendar month'):
        review_annual_release(db, **args(date(2026, 2, 1), date(2026, 2, 28)))
    assert snapshot(db) == before
    # Even elapsed service does not substitute for the selected month's receipt.
    with pytest.raises(ValueError, match='Post completed source month'):
        review_annual_release(db, **args(date(2026, 2, 1), date(2026, 4, 1)))
    assert snapshot(db) == before


def test_release_replay_checks_authority_date_reason_and_digest(monthly):
    db, selection = held(monthly)
    review = review_annual_release(db, **selection)
    call = dict(operation_id='d' * 64, reviewed_digest=review['reviewed_digest'],
                human_confirmed=True, reason='Synthetic release', **selection)
    result = apply_annual_release(db, **call)
    before = snapshot(db)
    assert apply_annual_release(db, **call) == result
    changes = [dict(session_version=selection['session_version'] + 1),
               dict(expected_epoch='stale'), dict(session_id='different'),
               dict(actor_id=selection['actor_id'] + 10000),
               dict(evaluated_on=date(2026, 4, 2)), dict(reason='Changed'),
               dict(reviewed_digest='0' * 64), dict(human_confirmed=False)]
    for change in changes:
        with pytest.raises(ValueError):
            apply_annual_release(db, **(call | change))
        assert snapshot(db) == before


def test_release_stale_review_and_cross_year_refuse_without_mutation(monthly):
    db, selection = held(monthly)
    review = review_annual_release(db, **selection)
    before = snapshot(db)
    with pytest.raises(ValueError, match='evidence changed'):
        apply_annual_release(db, operation_id='d' * 64, reviewed_digest='0' * 64,
                             human_confirmed=True, reason='Synthetic release', **selection)
    with pytest.raises(ValueError, match='calendar baseline'):
        review_annual_release(db, **(selection | {'month_start': date(2025, 12, 1),
                                                 'evaluated_on': date(2026, 1, 1)}))
    assert snapshot(db) == before


def test_release_insert_failure_rolls_back_and_same_operation_can_retry(monthly):
    db, selection = held(monthly)
    review = review_annual_release(db, **selection)
    call = dict(operation_id='d' * 64, reviewed_digest=review['reviewed_digest'],
                human_confirmed=True, reason='Synthetic release', **selection)
    before = snapshot(db)

    def fail(mapper, connection, target):
        if target.action == 'leave_annual_released':
            assert snapshot(db)[1] > before[1]
            raise RuntimeError('synthetic release insert failure')

    event.listen(AuditLog, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError, match='synthetic release insert failure'):
            apply_annual_release(db, **call)
    finally:
        event.remove(AuditLog, 'before_insert', fail)
    assert snapshot(db) == before
    result = apply_annual_release(db, **call)
    assert len(result['ledger']) == 2
    assert snapshot(db)[0] == before[0]  # release does not post a new month
