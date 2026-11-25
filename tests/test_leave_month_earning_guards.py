"""Payroll no-effect, payroll-overlap refusal, rollback and replay-identity guards for monthly earning.

Synthetic data only. Kernel-level tests on real migrated PostgreSQL; reuses the existing
`monthly` fixture and `post` helper. Concurrency tests are NOT in this file (need a committed fixture).
"""
from datetime import date
from hashlib import sha256
import json
import pytest
from sqlalchemy import func, select, text
from app.leave_month_earning import review_month_earning, apply_month_earning
from app.models import (AuditLog, LeaveBalanceTransaction, LeaveMonthClose, LeaveMonthEarning,
    PayrollItem, PayrollRun, AttendanceMonth, AttendanceDailyRecord)
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401

FEB = (date(2026, 2, 1), date(2026, 2, 28))
TABLES = ('payroll_runs', 'payroll_items', 'attendance_months', 'attendance_daily_records', 'leave_month_close')


def counts(db):
    return {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in
        (LeaveMonthEarning, LeaveBalanceTransaction, AuditLog, PayrollRun, PayrollItem,
         AttendanceMonth, AttendanceDailyRecord, LeaveMonthClose)}


def neighbor_hash(db):
    digest = sha256()
    for table in TABLES:
        rows = db.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY 1')).scalars().all()
        digest.update(table.encode() + json.dumps(rows).encode())
    return digest.hexdigest()


def ledger_values(db):
    return sorted(db.execute(select(LeaveBalanceTransaction.id, LeaveBalanceTransaction.source_operation_key,
        LeaveBalanceTransaction.leave_type_id, LeaveBalanceTransaction.source_digest)).all())


def payroll_run(db, status, start, end):
    run = PayrollRun(period_start=start, period_end=end, pay_date=end, status=status, created_by='synthetic')
    db.add(run); db.flush()
    return run


def test_earning_and_release_do_not_touch_payroll_attendance_or_close(monthly):
    db, _e, _kinds, args = monthly
    before_hash = neighbor_hash(db); before = counts(db)
    post(db, args(*FEB))
    post(db, args(date(2026, 3, 1), date(2026, 4, 1)), 'b' * 64)  # also releases held Feb annual
    after = counts(db)
    for key in ('payroll_runs', 'payroll_items', 'attendance_months', 'attendance_daily_records', 'leave_month_close'):
        assert after[key] == before[key]
    assert after['leave_month_earnings'] == before['leave_month_earnings'] + 2
    assert after['leave_balance_transactions'] > before['leave_balance_transactions']
    assert neighbor_hash(db) == before_hash


@pytest.mark.parametrize('status', ['approved', 'closed'])
def test_approved_or_closed_overlapping_payroll_refuses_before_any_mutation(monthly, status):
    db, _e, _kinds, args = monthly
    selection = args(*FEB)
    review = review_month_earning(db, **selection)
    payroll_run(db, status, date(2026, 2, 15), date(2026, 3, 14))
    before = counts(db); ledger = ledger_values(db); neighbors = neighbor_hash(db)
    with pytest.raises(ValueError, match='Approved/closed payroll'):
        review_month_earning(db, **selection)
    with pytest.raises(ValueError, match='Approved/closed payroll'):
        apply_month_earning(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
            human_confirmed=True, reason='Synthetic reviewed earning', **selection)
    assert counts(db) == before and ledger_values(db) == ledger and neighbor_hash(db) == neighbors


@pytest.mark.parametrize('status,start,end', [('draft', date(2026, 2, 1), date(2026, 2, 28)),
    ('reopened', date(2026, 2, 1), date(2026, 2, 28)),
    ('approved', date(2026, 3, 1), date(2026, 3, 31)), ('closed', date(2026, 1, 1), date(2026, 1, 31))])
def test_draft_reopened_or_non_overlapping_payroll_does_not_refuse(monthly, status, start, end):
    db, _e, _kinds, args = monthly
    payroll_run(db, status, start, end)
    before_runs = db.execute(text('SELECT row_to_json(t)::text FROM payroll_runs t ORDER BY 1')).scalars().all()
    post(db, args(*FEB))
    assert db.execute(text('SELECT row_to_json(t)::text FROM payroll_runs t ORDER BY 1')).scalars().all() == before_runs


def test_database_exception_inside_apply_rolls_back_all_monthly_effects(monthly, monkeypatch):
    db, _e, _kinds, args = monthly
    selection = args(*FEB)
    review = review_month_earning(db, **selection)
    before = counts(db); ledger = ledger_values(db); neighbors = neighbor_hash(db)

    def failing_audit(*a, **k):
        # Ledger rows and the receipt are already flushed in the nested transaction when this runs.
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) > before['leave_balance_transactions']
        raise RuntimeError('synthetic database failure after partial flush')
    from sqlalchemy import event
    event.listen(AuditLog, 'before_insert', failing_audit)
    try:
        with pytest.raises(RuntimeError, match='synthetic database failure'):
            apply_month_earning(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
                human_confirmed=True, reason='Synthetic reviewed earning', **selection)
    finally:
        event.remove(AuditLog, 'before_insert', failing_audit)
    assert counts(db) == before and ledger_values(db) == ledger and neighbor_hash(db) == neighbors
    # The failed attempt left nothing: a clean retry with the same operation id succeeds.
    result = apply_month_earning(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
        human_confirmed=True, reason='Synthetic reviewed earning', **selection)
    assert result['ledger'] and db.scalar(select(func.count()).select_from(LeaveMonthEarning)) == before['leave_month_earnings'] + 1


def test_replay_returns_identical_result_without_new_rows_then_rechecks_identity(monthly):
    db, _e, _kinds, args = monthly
    selection = args(*FEB)
    review, result = post(db, selection)
    snapshot = counts(db); ledger = ledger_values(db)
    again = apply_month_earning(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
        human_confirmed=True, reason='Synthetic reviewed earning', **selection)
    assert again == result and counts(db) == snapshot and ledger_values(db) == ledger
    for key, value in (('session_version', selection['session_version'] + 1), ('expected_epoch', 'f' * 32),
            ('session_id', 'synthetic-other-session'), ('actor_id', selection['actor_id'] + 100000)):
        with pytest.raises(ValueError):
            apply_month_earning(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
                human_confirmed=True, reason='Synthetic reviewed earning', **(selection | {key: value}))
        assert counts(db) == snapshot and ledger_values(db) == ledger
    with pytest.raises(ValueError, match='identity changed|reviewed correction'):  # same op id, different reason
        apply_month_earning(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
            human_confirmed=True, reason='Different reason', **selection)
    assert counts(db) == snapshot
