"""Group 4 / 4ب: regular-advance cap = 25% of (basic + that month's performance bonus when the month's
payroll item exists, else basic only). SQLite stand-in DB like the g4 advance characterization tests.
Design relayed via Main (this docstring is not an approval record)."""
from datetime import date
from decimal import Decimal as D

import pytest

import app.advance_workflow as aw
from app.models import PayrollItem, PayrollRun
from tests.test_g4_payroll_advance_characterization import db, mk  # noqa: F401


def month_item(db, performance, start=date(2026, 11, 1), employee_id=1):
    run = PayrollRun(period_start=start, period_end=date(2026, 11, 30), pay_date=date(2026, 12, 5),
                     status="draft", created_by="t")
    db.add(run); db.flush()
    db.add(PayrollItem(payroll_run_id=run.id, employee_id=employee_id, employee_code_snapshot="E1",
                       employee_name_snapshot="A", department_name_snapshot="d", position_snapshot="p",
                       salary_basis=D("6000"), base_salary=D("6000"), daily_hours=D("8"),
                       performance_bonus=D(performance), net_pay=D("0"), overtime_amount=D("0")))
    db.flush()


def test_no_month_item_means_basic_only(db):
    mk(db, "1500.00")
    with pytest.raises(ValueError, match="limit is 1500.00"):
        mk(db, "0.01")


def test_month_item_adds_performance_bonus_to_cap(db):
    month_item(db, "600.00")                       # (6000+600)*25% = 1650
    mk(db, "1650.00")
    with pytest.raises(ValueError, match="limit is 1650.00"):
        mk(db, "0.01")


def test_other_months_item_is_ignored_and_over_cap_by_a_cent_refused(db):
    month_item(db, "600.00", start=date(2026, 10, 1))
    with pytest.raises(ValueError, match="limit is 1500.00"):
        mk(db, "1500.01")


def test_approval_rechecks_with_same_basis(db):
    month_item(db, "600.00")
    req = mk(db, "1650.00")
    aw.approve_advance(db, req, "hr")              # same basis at approval: no refusal
    assert req.status == "approved"


def test_installment_advance_still_not_capped(db):
    month_item(db, "0.00")
    req = mk(db, "9000.00", kind="installment")
    assert req.amount == D("9000.00")
