"""Design relayed via Main (this docstring is not an approval record): a mid-month basic-salary change is prorated by calendar days.
Oct 2026 (31 days), 9000 until Oct 15 and 12000 from Oct 16: (9000*15 + 12000*16)/31 = 10548.39.
A mid-month daily_hours change stays blocked (no recorded policy). SQLite payroll_client route fixture."""
from datetime import date
from decimal import Decimal
from sqlalchemy import select
from app.models import Employee, EmployeeChange, PayrollItem
from tests.test_payroll_routes import payroll_client, login, csrf  # noqa: F401


def _change(client, eff, new="12000.00", old="9000.00"):
    with client.test_session_factory.begin() as db:
        e = db.get(Employee, client.test_employee_id)
        e.basic_salary = Decimal(new)
        db.add(EmployeeChange(employee_id=e.id, field_name="basic_salary", old_value=old, new_value=new,
                              effective_date=eff, reason="Synthetic mid-month salary change"))


def _create(client):
    token = csrf(client.get("/payroll"))
    return client.post("/payroll", data={"csrf_token": token, "year": "2026", "month": "10"}, follow_redirects=False)


def test_mid_month_salary_change_is_prorated_by_calendar_days(payroll_client):
    c = payroll_client; login(c)
    _change(c, date(2026, 10, 16))
    r = _create(c)
    assert "error" not in r.headers["location"]
    with c.test_session_factory() as db:
        item = db.scalar(select(PayrollItem))
        assert item.base_salary == Decimal("10548.39")
        assert item.salary_basis == Decimal("9000.00")           # rate at period start (other formulas unchanged)
    page = c.get(r.headers["location"])
    assert "الكشف مبني على اللقطة القديمة" not in page.text      # not flagged stale


def test_change_on_first_day_is_not_mid_month_and_uses_new_rate(payroll_client):
    c = payroll_client; login(c)
    _change(c, date(2026, 10, 1))
    r = _create(c)
    assert "error" not in r.headers["location"]
    with c.test_session_factory() as db:
        item = db.scalar(select(PayrollItem))
        assert item.base_salary == Decimal("12000.00") and item.salary_basis == Decimal("12000.00")


def test_stale_flag_when_change_entered_after_run_creation(payroll_client):
    c = payroll_client; login(c)
    r = _create(c)
    assert "error" not in r.headers["location"]
    _change(c, date(2026, 10, 16))
    page = c.get(r.headers["location"])
    assert "الكشف مبني على اللقطة القديمة" in page.text         # item still carries the unprorated base
    # The flag clears once the item carries the prorated base (fails on base: any mid-month change was flagged).
    with c.test_session_factory.begin() as db:
        it = db.scalar(select(PayrollItem)); it.base_salary = Decimal("10548.39")
    page = c.get(r.headers["location"])
    assert "الكشف مبني على اللقطة القديمة" not in page.text
