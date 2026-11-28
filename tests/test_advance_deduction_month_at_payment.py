"""The first installment month follows the REAL payment date HR enters (Mo's text of 27 Nov 2026 23:51:32, read in
meaning, relayed by Main); window through the whole 5th of the next month, then current month with reason and label.
Synthetic data only."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.advance_workflow import approve_advance, mark_advance_paid
from app.db import SessionLocal
from app.models import AdvanceInstallment, AdvanceRequest, Department, Employee, PayrollRun

_CREATED = []
_RUNS = []


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal.begin() as db:
        db.execute(text("SET LOCAL session_replication_role = replica"))
        for run_id in _RUNS:
            db.execute(text("DELETE FROM payroll_runs WHERE id = :r"), {"r": run_id})
        for request_id, employee_id, dept_id in _CREATED:
            db.execute(text("DELETE FROM audit_logs WHERE entity_type='advance_request' AND entity_id = :r"), {"r": str(request_id)})
            db.execute(text("DELETE FROM advance_installments WHERE advance_request_id = :r"), {"r": request_id})
            db.execute(text("DELETE FROM advance_requests WHERE id = :r"), {"r": request_id})
            db.execute(text("DELETE FROM employees WHERE id = :e"), {"e": employee_id})
            db.execute(text("DELETE FROM departments WHERE id = :d"), {"d": dept_id})
    _CREATED.clear()
    _RUNS.clear()


def _approved(kind="installment", count=3, amount="3000", request_date=date(2031, 11, 16)):
    code = "PAYM-" + uuid4().hex[:8]
    with SessionLocal.begin() as db:
        dept = Department(name="Pay month " + code, active=True)
        db.add(dept)
        db.flush()
        employee = Employee(code=code, full_name="Synthetic " + code, department_id=dept.id, position="P",
                            hire_date=date(2026, 1, 1), status="active",
                            basic_salary=Decimal("10000"), daily_hours=Decimal("8"))
        db.add(employee)
        db.flush()
        request = AdvanceRequest(employee_id=employee.id, request_date=request_date,
                                 payroll_month=date(request_date.year, request_date.month, 1),
                                 amount=Decimal(amount), advance_type=kind, salary_basis=Decimal("10000"),
                                 reason="synthetic", status="pending", created_by="synthetic")
        db.add(request)
        db.flush()
        _CREATED.append((request.id, employee.id, dept.id))
        approve_advance(db, request, "synthetic", count)
        return request.id


def _months(request_id):
    with SessionLocal() as db:
        rows = db.scalars(select(AdvanceInstallment).where(AdvanceInstallment.advance_request_id == request_id)
                          .order_by(AdvanceInstallment.installment_number)).all()
        return [(r.payroll_month, r.scheduled_payment_date, Decimal(r.amount)) for r in rows]


def _pay(request_id, paid_on, today, reason=None, delay=0):
    with SessionLocal.begin() as db:
        return mark_advance_paid(db, db.get(AdvanceRequest, request_id), "synthetic",
                                 paid_on=paid_on, reason=reason, today=today, start_delay_months=delay)


def _row(request_id):
    with SessionLocal() as db:
        r = db.get(AdvanceRequest, request_id)
        return r.status, r.payroll_month, r.paid_date, r.retroactive_for_month, r.late_payment_reason


def test_before_payment_schedule_follows_request_then_payment_date_decides():
    rid = _approved()
    assert _months(rid)[0][0] == date(2031, 11, 1)
    _pay(rid, date(2031, 11, 28), date(2031, 11, 28))
    rows = _months(rid)
    assert [r[0] for r in rows] == [date(2031, 11, 1), date(2031, 12, 1), date(2032, 1, 1)]
    assert rows[0][1] == date(2031, 12, 5)
    assert sum(r[2] for r in rows) == Decimal("3000.00")


@pytest.mark.parametrize("paid,today", [(date(2031, 12, 2), date(2031, 12, 2)), (date(2031, 12, 5), date(2031, 12, 6))])
def test_paid_in_december_is_december_even_when_recorded_later(paid, today):
    rid = _approved()
    assert _pay(rid, paid, today) == date(2031, 12, 1)
    st, month, pdate, retro, reason = _row(rid)
    assert (st, pdate, retro) == ("paid", paid, None)


def test_payment_in_november_recorded_through_the_5th_stays_november_without_label():
    rid = _approved()
    assert _pay(rid, date(2031, 11, 25), date(2031, 12, 5)) == date(2031, 11, 1)
    st, month, pdate, retro, reason = _row(rid)
    assert (month, retro, reason) == (date(2031, 11, 1), None, None)
    assert _months(rid)[0][0] == date(2031, 11, 1)


def test_after_the_5th_needs_a_reason_and_goes_to_the_current_month_with_label():
    rid = _approved()
    with pytest.raises(ValueError, match="reason is required"):
        _pay(rid, date(2031, 11, 25), date(2031, 12, 6))
    assert _row(rid)[0] == "approved"
    assert _pay(rid, date(2031, 11, 25), date(2031, 12, 6), reason="forgotten") == date(2031, 12, 1)
    st, month, pdate, retro, reason = _row(rid)
    assert (month, pdate, retro, reason) == (date(2031, 12, 1), date(2031, 11, 25), date(2031, 11, 1), "forgotten")
    rows = _months(rid)
    assert [r[0] for r in rows] == [date(2031, 12, 1), date(2032, 1, 1), date(2032, 2, 1)]
    assert rows[0][1] == date(2032, 1, 5)


def test_future_date_and_date_before_request_are_refused():
    rid = _approved()
    with pytest.raises(ValueError, match="future"):
        _pay(rid, date(2031, 12, 3), date(2031, 12, 2))
    with pytest.raises(ValueError, match="before the request"):
        _pay(rid, date(2031, 11, 15), date(2031, 12, 2))
    assert _row(rid)[0] == "approved"


def test_closed_run_is_a_hard_lock_even_inside_the_window():
    rid = _approved(count=2, amount="1000")
    with SessionLocal.begin() as db:
        run = PayrollRun(period_start=date(2031, 11, 1), period_end=date(2031, 11, 30), pay_date=date(2031, 12, 5),
                         status="closed", created_by="synthetic")
        db.add(run)
        db.flush()
        _RUNS.append(run.id)
    with pytest.raises(ValueError, match="reason is required"):
        _pay(rid, date(2031, 11, 29), date(2031, 12, 3))
    _pay(rid, date(2031, 11, 29), date(2031, 12, 3), reason="run already closed")
    assert _row(rid)[3] == date(2031, 11, 1)
    assert [r[0] for r in _months(rid)] == [date(2031, 12, 1), date(2032, 1, 1)]


def test_year_end_rolls_over():
    rid = _approved(count=2, amount="1000")
    _pay(rid, date(2031, 12, 30), date(2031, 12, 30))
    assert [r[0] for r in _months(rid)] == [date(2031, 12, 1), date(2032, 1, 1)]


def test_used_installments_are_never_moved():
    rid = _approved(count=2, amount="1000")
    with SessionLocal.begin() as db:
        row = db.scalars(select(AdvanceInstallment).where(AdvanceInstallment.advance_request_id == rid)).first()
        row.status = "applied"
    with pytest.raises(ValueError, match="already used"):
        _pay(rid, date(2031, 12, 2), date(2031, 12, 2))
    assert _row(rid)[0] == "approved"


def test_regular_advance_cap_is_checked_in_the_real_deduction_month():
    rid = _approved(kind="regular", count=1, amount="2500")
    other = _approved(kind="regular", count=1, amount="2500", request_date=date(2031, 12, 16))
    with SessionLocal.begin() as db:
        db.get(AdvanceRequest, rid).employee_id = db.get(AdvanceRequest, other).employee_id
    with pytest.raises(ValueError, match="limit"):
        _pay(rid, date(2031, 12, 3), date(2031, 12, 3))
    st, month, *_ = _row(rid)
    assert (st, month) == ("approved", date(2031, 11, 1))


def test_installment_advance_may_start_later_never_earlier():
    rid = _approved()
    assert _pay(rid, date(2031, 12, 2), date(2031, 12, 2), delay=2) == date(2032, 2, 1)
    rows = _months(rid)
    assert [r[0] for r in rows] == [date(2032, 2, 1), date(2032, 3, 1), date(2032, 4, 1)]
    assert rows[0][1] == date(2032, 3, 5)
    assert _row(rid)[3] is None


def test_delay_counts_from_the_placed_month_in_the_closed_month_case():
    rid = _approved()
    assert _pay(rid, date(2031, 11, 25), date(2031, 12, 8), reason="late", delay=1) == date(2032, 1, 1)
    assert _row(rid)[3] == date(2031, 11, 1)


@pytest.mark.parametrize("delay", [-1, 3])
def test_delay_out_of_range_is_refused(delay):
    rid = _approved()
    with pytest.raises(ValueError, match="between 0 and 2"):
        _pay(rid, date(2031, 12, 2), date(2031, 12, 2), delay=delay)
    assert _row(rid)[0] == "approved"


def test_regular_advance_cannot_start_later():
    rid = _approved(kind="regular", count=1, amount="1000")
    with pytest.raises(ValueError, match="Only an installment"):
        _pay(rid, date(2031, 11, 20), date(2031, 11, 20), delay=1)
    assert _row(rid)[0] == "approved"


def test_closed_current_month_run_sends_a_current_month_payment_to_the_next_month_with_label():
    rid = _approved(count=2, amount="1000")
    with SessionLocal.begin() as db:
        run = PayrollRun(period_start=date(2031, 12, 1), period_end=date(2031, 12, 31), pay_date=date(2032, 1, 5),
                         status="closed", created_by="synthetic")
        db.add(run)
        db.flush()
        _RUNS.append(run.id)
    with pytest.raises(ValueError, match="reason is required"):
        _pay(rid, date(2031, 12, 10), date(2031, 12, 20))
    assert _pay(rid, date(2031, 12, 10), date(2031, 12, 20), reason="December run already closed") == date(2032, 1, 1)
    assert _row(rid)[3] == date(2031, 12, 1)
