"""Bonus and deduction month placement: open until the 5th of the next month (the whole 5th), then the
current month with a retroactive label. Rule read in meaning from Mo's voice notes of 27 Nov 2026 23:40/23:41,
relayed by Main. Synthetic data only."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.models import Department, Employee, PayrollRun
from app.open_month import is_month_open, place_month
from app.staff_actions_workflow import approve_staff_action, create_staff_action

_IDS = {"emp": [], "dept": [], "run": [], "act": []}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with SessionLocal.begin() as db:
        db.execute(text("SET LOCAL session_replication_role = replica"))
        for a in _IDS["act"]:
            db.execute(text("DELETE FROM audit_logs WHERE entity_type='staff_action' AND entity_id=:a"), {"a": str(a)})
            db.execute(text("DELETE FROM staff_actions WHERE id=:a"), {"a": a})
        for r in _IDS["run"]:
            db.execute(text("DELETE FROM payroll_items WHERE payroll_run_id = :r"), {"r": r})
            db.execute(text("DELETE FROM audit_logs WHERE entity_type='payroll_run' AND entity_id = :r"), {"r": str(r)})
            db.execute(text("DELETE FROM payroll_runs WHERE id=:r"), {"r": r})
        for e in _IDS["emp"]:
            db.execute(text("DELETE FROM employees WHERE id=:e"), {"e": e})
        for d in _IDS["dept"]:
            db.execute(text("DELETE FROM departments WHERE id=:d"), {"d": d})
    for v in _IDS.values():
        v.clear()


def _employee(db):
    code = "OPEN-" + uuid4().hex[:8]
    dept = Department(name="Open month " + code, active=True)
    db.add(dept)
    db.flush()
    emp = Employee(code=code, full_name="Synthetic " + code, department_id=dept.id, position="P",
                   hire_date=date(2026, 1, 1), status="active", basic_salary=Decimal("10000"),
                   daily_hours=Decimal("8"))
    db.add(emp)
    db.flush()
    _IDS["emp"].append(emp.id)
    _IDS["dept"].append(dept.id)
    return emp.id


def _run(db, month, status):
    end = date(month.year, month.month, 28)
    run = PayrollRun(period_start=month, period_end=end, pay_date=date(month.year, month.month, 28),
                     status=status, created_by="synthetic")
    db.add(run)
    db.flush()
    _IDS["run"].append(run.id)


def _make(db, emp, month, today):
    item = create_staff_action(db, employee_id=emp, kind="sanction", category="salary_deduction", title="t",
                               is_financial=True, amount=Decimal("500"), payroll_month=month,
                               reason="synthetic reason", actor="synthetic", today=today)
    _IDS["act"].append(item.id)
    return item


NOV, DEC = date(2031, 11, 1), date(2031, 12, 1)


@pytest.mark.parametrize("today,placed,retro", [
    (date(2031, 11, 20), NOV, None),
    (date(2031, 12, 1), NOV, None),
    (date(2031, 12, 5), NOV, None),          # the 5th itself is still November
    (date(2031, 12, 6), DEC, NOV),           # after the 5th: current month, labelled
    (date(2032, 1, 2), date(2032, 1, 1), NOV),
])
def test_place_month_cutoff(today, placed, retro):
    with SessionLocal.begin() as db:
        assert place_month(db, NOV, today) == (placed, retro)


def test_future_month_is_kept():
    with SessionLocal.begin() as db:
        assert place_month(db, date(2032, 3, 1), date(2031, 12, 6)) == (date(2032, 3, 1), None)


def test_closed_run_beats_the_date():
    with SessionLocal.begin() as db:
        _run(db, date(2033, 11, 1), "closed")
        assert not is_month_open(db, date(2033, 11, 1), date(2033, 12, 2))
        assert place_month(db, date(2033, 11, 1), date(2033, 12, 2)) == (date(2033, 12, 1), date(2033, 11, 1))


def test_draft_or_reopened_run_stays_open_until_the_5th():
    with SessionLocal.begin() as db:
        _run(db, date(2034, 11, 1), "draft")
        assert is_month_open(db, date(2034, 11, 1), date(2034, 12, 5))
        assert not is_month_open(db, date(2034, 11, 1), date(2034, 12, 6))


def test_create_after_cutoff_moves_and_labels_with_reason_kept():
    with SessionLocal.begin() as db:
        emp = _employee(db)
        item = _make(db, emp, NOV, date(2031, 12, 8))
        assert (item.payroll_month, item.retroactive_for_month, item.reason) == (DEC, NOV, "synthetic reason")


def test_create_before_cutoff_keeps_month_without_label():
    with SessionLocal.begin() as db:
        emp = _employee(db)
        item = _make(db, emp, NOV, date(2031, 12, 3))
        assert (item.payroll_month, item.retroactive_for_month) == (NOV, None)


def test_approve_after_cutoff_moves_a_draft_made_earlier():
    with SessionLocal.begin() as db:
        emp = _employee(db)
        item = _make(db, emp, NOV, date(2031, 11, 25))
        approve_staff_action(db, item, "synthetic", today=date(2031, 12, 9))
        assert (item.status, item.payroll_month, item.retroactive_for_month) == ("approved", DEC, NOV)


def test_non_financial_action_has_no_label():
    with SessionLocal.begin() as db:
        emp = _employee(db)
        item = create_staff_action(db, employee_id=emp, kind="sanction", category="verbal_notice", title="t",
                                   is_financial=False, amount=None, payroll_month=None, reason="r",
                                   actor="synthetic", today=date(2031, 12, 9))
        _IDS["act"].append(item.id)
        assert item.retroactive_for_month is None


@pytest.mark.parametrize("status", ["approved", "closed"])
def test_current_month_entry_with_a_locked_run_goes_to_the_next_month_with_label(status):
    with SessionLocal.begin() as db:
        emp = _employee(db)
        _run(db, DEC, status)
        placed, retro = place_month(db, DEC, date(2031, 12, 10))
        assert (placed, retro) == (date(2032, 1, 1), DEC)
        item = _make(db, emp, DEC, date(2031, 12, 10))
        assert (item.payroll_month, item.retroactive_for_month) == (date(2032, 1, 1), DEC)


def _draft_item(db, emp, month):
    from app.models import PayrollItem
    run = PayrollRun(period_start=month, period_end=date(month.year, month.month, 28),
                     pay_date=date(month.year, month.month, 28), status="draft", created_by="synthetic")
    db.add(run)
    db.flush()
    _IDS["run"].append(run.id)
    item = PayrollItem(payroll_run_id=run.id, employee_id=emp, employee_code_snapshot="X", employee_name_snapshot="X",
                       department_name_snapshot="X", position_snapshot="P", salary_basis=Decimal("10000"),
                       base_salary=Decimal("10000"), daily_hours=Decimal("8"), net_pay=Decimal("0.00"))
    db.add(item)
    db.flush()
    from app.payroll_workflow import recompute_payroll_item
    recompute_payroll_item(item, run)
    return run, item


def test_entry_approved_while_the_month_is_a_draft_is_added_to_the_draft_once():
    from app.payroll_workflow import refresh_draft_run_entries
    with SessionLocal.begin() as db:
        emp = _employee(db)
        run, item = _draft_item(db, emp, DEC)
        before = Decimal(item.net_pay)
        action = _make(db, emp, DEC, date(2031, 12, 3))
        approve_staff_action(db, action, "synthetic", today=date(2031, 12, 3))
        assert action.payroll_run_id == run.id
        assert Decimal(item.staff_sanction_amount) == Decimal("500.00")
        assert Decimal(item.net_pay) == before - Decimal("500.00")
        assert refresh_draft_run_entries(db, run, "synthetic") == 0      # idempotent
        assert Decimal(item.net_pay) == before - Decimal("500.00")


def test_a_run_that_is_approved_is_not_changed_by_refresh():
    from app.payroll_workflow import refresh_draft_run_entries
    with SessionLocal.begin() as db:
        emp = _employee(db)
        run, item = _draft_item(db, emp, DEC)
        run.status = "approved"
        db.flush()
        assert refresh_draft_run_entries(db, run, "synthetic") == 0


def test_entry_for_an_employee_without_an_item_moves_to_the_next_month_when_the_run_is_approved():
    from app.payroll_workflow import move_left_out_entries_to_next_month
    with SessionLocal.begin() as db:
        emp = _employee(db)
        other = _employee(db)
        run, _item = _draft_item(db, other, DEC)
        action = _make(db, emp, DEC, date(2031, 12, 3))
        approve_staff_action(db, action, "synthetic", today=date(2031, 12, 3))
        assert action.payroll_run_id is None
        assert move_left_out_entries_to_next_month(db, run, "synthetic") == 1
        assert (action.payroll_month, action.retroactive_for_month) == (date(2032, 1, 1), DEC)
        assert move_left_out_entries_to_next_month(db, run, "synthetic") == 0



def test_attendance_adjustment_suggested_amount_and_placement():
    from app.staff_actions_workflow import create_attendance_adjustment, suggested_adjustment_amount
    assert suggested_adjustment_amount(Decimal("10000"), 2, date(2031, 11, 1)) == Decimal("666.67")
    assert suggested_adjustment_amount(Decimal("10000"), 1, date(2032, 2, 1)) == Decimal("344.83")
    with SessionLocal.begin() as db:
        emp = _employee(db)
        a = create_attendance_adjustment(db, employee_id=emp, attendance_month=date(2031, 11, 17), adjustment="overpaid",
                                         days=2, amount=None, reason="synthetic", actor="synthetic", today=date(2031, 12, 10))
        _IDS["act"].append(a.id)
        assert (a.kind, a.category, a.amount, a.status) == ("sanction", "attendance_adjustment", Decimal("666.67"), "draft")
        assert a.payroll_month == DEC and a.retroactive_for_month == NOV
        b = create_attendance_adjustment(db, employee_id=emp, attendance_month=NOV, adjustment="missed",
                                         days=Decimal("0.5"), amount=Decimal("100"), reason="synthetic", actor="synthetic",
                                         today=date(2031, 12, 3))
        _IDS["act"].append(b.id)
        assert (b.kind, b.amount, b.payroll_month, b.retroactive_for_month) == ("reward", Decimal("100.00"), DEC, NOV)


@pytest.mark.parametrize("kwargs", [
    {"attendance_month": DEC},                  # not an ended month
    {"adjustment": "other"},
    {"days": 0},
    {"days": Decimal("1.3")},
    {"days": 31},
    {"reason": "  "},
])
def test_attendance_adjustment_refuses_bad_input(kwargs):
    from app.staff_actions_workflow import create_attendance_adjustment
    base = dict(attendance_month=NOV, adjustment="overpaid", days=1, amount=None, reason="r", actor="synthetic",
                today=date(2031, 12, 10))
    base.update(kwargs)
    with SessionLocal.begin() as db:
        base["employee_id"] = _employee(db)
        with pytest.raises(ValueError):
            create_attendance_adjustment(db, **base)


@pytest.mark.parametrize("status", ["approved", "closed"])
def test_attendance_adjustment_keeps_the_wrong_month_when_the_current_month_is_locked(status):
    from app.staff_actions_workflow import create_attendance_adjustment
    with SessionLocal.begin() as db:
        emp = _employee(db)
        _run(db, DEC, status)
        a = create_attendance_adjustment(db, employee_id=emp, attendance_month=NOV, adjustment="overpaid", days=1,
                                         amount=None, reason="synthetic", actor="synthetic", today=date(2031, 12, 10))
        _IDS["act"].append(a.id)
        assert (a.payroll_month, a.retroactive_for_month) == (date(2032, 1, 1), NOV)
