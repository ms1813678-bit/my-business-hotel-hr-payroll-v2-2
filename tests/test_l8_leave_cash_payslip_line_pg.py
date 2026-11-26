"""2ذ (owner decision Mo 2026-11-26): leave cash-out is its own payslip line, added to net pay
of the payroll item the verified receipts point to. Real PG, synthetic, source amounts."""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from app.payroll_workflow import _sync_leave_cash_amount, recompute_payroll_item
from app.models import PayrollRun
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401
from tests.test_leave_cash_out import auth_of, cash, payroll


def test_cash_lands_as_separate_line_in_net_once_and_only_on_its_own_item(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    run, item = payroll(db, e, status='draft')
    _r2, other = payroll(db, e, status='draft', start=date(2026, 4, 1), end=date(2026, 4, 30))
    recompute_payroll_item(item, run); recompute_payroll_item(other, db.get(PayrollRun, other.payroll_run_id))
    db.flush(); net0, other_net0 = item.net_pay, other.net_pay
    assert item.leave_cash_amount == 0
    cash(db, auth, e, kinds['W'], item, '2/7', operation='3' * 64)
    cash(db, auth, e, kinds['W'], item, '2/7', operation='4' * 64)
    db.flush()
    recompute_payroll_item(item, run)
    assert item.net_pay == net0                       # not synced yet: payroll rows stay as they were
    _sync_leave_cash_amount(db, item); recompute_payroll_item(item, run)
    assert item.leave_cash_amount == Decimal('68.58') and item.net_pay == net0 + Decimal('68.58')
    _sync_leave_cash_amount(db, item); recompute_payroll_item(item, run)       # idempotent, no double count
    assert item.net_pay == net0 + Decimal('68.58')
    _sync_leave_cash_amount(db, other); recompute_payroll_item(other, db.get(PayrollRun, other.payroll_run_id))
    assert other.leave_cash_amount == 0 and other.net_pay == other_net0


def test_database_refuses_negative_leave_cash_line(monthly):
    db, e, kinds, args = monthly
    run, item = payroll(db, e, status='draft')
    with pytest.raises(IntegrityError):
        db.execute(text('UPDATE payroll_items SET leave_cash_amount = -1 WHERE id = :i'), {'i': item.id})
        db.flush()


@pytest.mark.parametrize('status', ['approved', 'closed'])
def test_new_cash_out_refused_on_approved_or_closed_item_without_writes(monthly, status):
    from app.leave_cash_out import review_cash_out
    from tests.test_leave_cash_out import counts
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    _run, item = payroll(db, e, status=status)
    before = counts(db)
    with pytest.raises(ValueError, match='draft or reopened payroll item'):
        review_cash_out(db, employee_id=e.id, leave_type_id=kinds['W'], days='2/7',
                        payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)
    assert counts(db) == before


def test_reopened_item_still_takes_cash_and_exact_retry_survives_later_approval(monthly):
    from app.leave_cash_out import apply_cash_out
    from tests.test_leave_cash_out import REASON
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    run, item = payroll(db, e, status='reopened')
    review, result = cash(db, auth, e, kinds['W'], item, '2/7', operation='5' * 64)
    run.status = 'approved'; db.flush()          # month approved after the cash-out was recorded
    again = apply_cash_out(db, operation_id='5' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True,
                           reason=REASON, employee_id=e.id, leave_type_id=kinds['W'], days='2/7',
                           payroll_item_id=item.id, evaluated_on=date(2026, 4, 2), **auth)
    assert again == result


def test_http_approve_route_applies_the_cash_line_to_net_pay(monthly, monkeypatch):
    """The real POST /payroll/runs/{id}/approve route (auth/CSRF dependencies stubbed, DB session is the test's own):
    approval must sync the verified cash-out into leave_cash_amount and net pay, once."""
    import contextlib
    from fastapi.testclient import TestClient
    from app.csrf import csrf_protect
    from app.dependencies import require_payroll_access
    from app.main import app
    from app.models import AttendanceMonth
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))
    auth = auth_of(args)
    run, item = payroll(db, e, status='draft')
    recompute_payroll_item(item, run); db.flush()
    net0 = item.net_pay
    cash(db, auth, e, kinds['W'], item, '2/7', operation='6' * 64)
    db.add(AttendanceMonth(period_start=run.period_start, period_end=run.period_end, status='approved', created_by='synthetic'))
    db.flush()
    assert item.leave_cash_amount == 0 and item.net_pay == net0          # nothing synced before approval
    class Factory:
        def begin(self): return contextlib.nullcontext(db)
    monkeypatch.setattr('app.main.SessionLocal', Factory())
    # the test's own session shares one transaction with the route, so the cross-connection restore gate is bypassed
    monkeypatch.setattr('app.restore_lock.database_is_postgres', lambda: False)
    app.dependency_overrides[require_payroll_access] = lambda: type('A', (), {'username': 'synthetic-payroll'})()
    async def no_csrf(): return None
    app.dependency_overrides[csrf_protect] = no_csrf
    try:
        r = TestClient(app).post(f'/payroll/runs/{run.id}/approve', follow_redirects=False)
    finally:
        app.dependency_overrides.pop(require_payroll_access, None); app.dependency_overrides.pop(csrf_protect, None)
    assert r.status_code == 303 and 'error' not in r.headers['location'], r.headers.get('location')
    db.flush(); db.refresh(run); db.refresh(item)       # the route's session is the test's own: flush, then re-read
    from app.models import AuditLog
    assert 'payroll_run_approved' in [a.action for a in db.scalars(select(AuditLog))]
    assert run.status == 'approved'
    assert item.leave_cash_amount == Decimal('34.29') and item.net_pay == net0 + Decimal('34.29')
