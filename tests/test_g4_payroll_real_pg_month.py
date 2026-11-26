"""Group 4 (4ت, wider subset): multi-employee month on real PG, hand-calculated.
Reuses the per-test migrated-DB fixture of test_g4_payroll_real_pg_flow (local-only
guard included). Characterization only; no policy claim.
Oct 2026 (31 days). E1: basic 6000, approved attendance day 09:00-17:00 actual
09:40-16:45 => late 40, early 15; chargeable (40-10)+(15-5)=40 min; 6000/(8*30)=25/h
=> 16.67. E1 net = 6000 + 300 reward - 1000 advance - 200 sanction - 16.67 = 5083.33.
E2: basic 3000 hired 2026-10-15 => 17/31 days => 1645.16; edited: absence 1d =
3000/31 = 96.77, insurance 100, tax 50 => 1398.39. Month total 6481.72."""
from datetime import date, datetime
from decimal import Decimal as D

from sqlalchemy import select

from tests.test_g4_payroll_real_pg_flow import _csrf, _login, pgapp  # noqa: F401


def _seed_extra(c):
    from app.models import (AttendanceDailyRecord, Employee, EmployeeServicePeriod)
    with c.factory() as db:
        e1 = db.scalar(select(Employee))
        dep = e1.department_id
        e2 = Employee(code="MY-PG-0002", full_name="PG Employee Two", department_id=dep, position="p",
                      hire_date=date(2026, 10, 15), status="active", basic_salary=D("3000.00"),
                      daily_hours=D("8"))
        db.add(e2); db.flush()
        db.add(EmployeeServicePeriod(employee_id=e2.id, start_date=date(2026, 10, 15), end_date=None,
                                     start_reason="synthetic"))
        db.add(AttendanceDailyRecord(
            employee_id=e1.id, work_date=date(2026, 10, 5),
            scheduled_start_at=datetime(2026, 10, 5, 9, 0), scheduled_end_at=datetime(2026, 10, 5, 17, 0),
            approved_clock_in=datetime(2026, 10, 5, 9, 40), approved_clock_out=datetime(2026, 10, 5, 16, 45),
            manual_clock_in=datetime(2026, 10, 5, 9, 40), manual_clock_out=datetime(2026, 10, 5, 16, 45),
            manual_entry_reason="synthetic", manual_entered_by="pg-admin",
            manual_entered_at=datetime.utcnow(), status="approved", approved_by="pg-admin",
            approved_at=datetime.utcnow()))
        db.commit()
        return e1.id, e2.id


def test_pg_two_employee_month_hand_oracle_and_edit_flow(pgapp):
    from app.models import PayrollItem, PayrollRun
    c = pgapp; _login(c)
    e1, e2 = _seed_extra(c)
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "10"}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    with c.factory() as db:
        items = {i.employee_id: i for i in db.scalars(select(PayrollItem))}
        a, b = items[e1], items[e2]
        assert (a.late_minutes, a.early_minutes, a.lateness_deduction) == (40, 15, D("16.67"))
        assert a.net_pay == D("5083.33")
        assert (b.base_salary, b.advance_deduction, b.staff_reward_amount) == (D("1645.16"), D("0.00"), D("0.00"))
        assert b.net_pay == D("1645.16")
        b_id = b.id
    r = c.post(f"/payroll/items/{b_id}/update", data={
        "csrf_token": tok, "absence_days": "1", "insurance_share": "100", "taxes": "50",
        "lateness_deduction": "0"}, follow_redirects=False)
    assert "error" not in r.headers["location"]
    for step in ("approve", "close"):
        assert c.post(f"/payroll/runs/{run_id}/{step}", data={"csrf_token": tok},
                      follow_redirects=False).status_code == 303
    with c.factory() as db:
        assert db.get(PayrollRun, run_id).status == "closed"
        items = {i.employee_id: i for i in db.scalars(select(PayrollItem))}
        assert items[e2].absence_deduction == D("96.77")
        assert items[e2].net_pay == D("1398.39")
        assert items[e1].net_pay == D("5083.33")
        assert sum((i.net_pay for i in items.values()), D("0")) == D("6481.72")


def test_pg_mid_month_salary_change_blocks_run_creation(pgapp):
    """Current source refuses to create the run (no rate guessed). Behavior pin."""
    from app.models import Employee, EmployeeChange, PayrollRun
    c = pgapp; _login(c)
    with c.factory() as db:
        e1 = db.scalar(select(Employee))
        cols = {k.name for k in EmployeeChange.__table__.columns if not k.nullable
                and k.default is None and k.server_default is None and not k.primary_key}
        vals = dict(employee_id=e1.id, field_name="basic_salary", old_value="6000.00",
                    new_value="7000.00", effective_date=date(2026, 10, 16), reason="synthetic")
        missing = cols - set(vals)
        assert not missing, missing
        db.add(EmployeeChange(**vals)); db.commit()
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "10"}, follow_redirects=False)
    with c.factory() as db:
        assert db.scalar(select(PayrollRun)) is None
    assert "error" in r.headers["location"]
