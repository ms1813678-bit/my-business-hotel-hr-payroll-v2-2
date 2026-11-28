"""A basic-salary change dated in a closed payroll month is refused (Mo, 28 Nov 2026 11:51:19, "منع", via Main).

Closed means the same as for entries: after the whole 5th of the next month, or the month's run is approved or
closed. The current month is never blocked, so HR always has a valid date."""
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Department, Employee, EmployeeChange, PayrollRun
from app.open_month import SALARY_CHANGE_CLOSED_MONTH_MESSAGE, first_of_month, salary_change_blocked, today_cairo
from app.security import hash_password

PW = "Salary-Block-Admin-Pass-123!"


def _months_back(n):
    m = first_of_month(today_cairo())
    for _ in range(n):
        m = date(m.year - (m.month == 1), (m.month - 2) % 12 + 1, 1)
    return m


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        dept = Department(name="Salary block dept", active=True)
        db.add(dept)
        db.flush()
        db.add(AdminAccount(username="sal-admin", password_hash=hash_password(PW), role="system_admin", active=True))
        emp = Employee(code="SAL-1", full_name="Synthetic Salary", department_id=dept.id, position="P",
                       hire_date=date(2020, 1, 1), status="active", basic_salary=Decimal("5000.00"),
                       daily_hours=Decimal("8"))
        db.add(emp)
        db.commit()
        ids = (emp.id, dept.id)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as c:
            c.emp_id, c.dept_id, c.factory = ids[0], ids[1], factory
            page = c.get("/login")
            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            assert c.post("/login", data={"username": "sal-admin", "password": PW, "csrf_token": token},
                          follow_redirects=False).status_code == 303
            yield c
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _post(c, **over):
    page = c.get("/employees")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    data = {"csrf_token": token, "full_name": "Synthetic Salary", "nationality": "egyptian", "department_id": c.dept_id,
            "position": "P", "basic_salary": "5000", "daily_hours": "8", "effective_date": today_cairo().isoformat(),
            "reason": "synthetic reason"}
    data.update(over)
    return c.post(f"/employees/{c.emp_id}/edit", data=data, follow_redirects=False)


def test_raise_dated_in_a_closed_month_is_refused_and_nothing_changes(client):
    old = _months_back(2).replace(day=10).isoformat()
    r = _post(client, basic_salary="6000", effective_date=old)
    assert r.status_code == 400
    assert SALARY_CHANGE_CLOSED_MONTH_MESSAGE in r.text
    with client.factory() as db:
        assert db.get(Employee, client.emp_id).basic_salary == Decimal("5000.00")
        assert db.scalars(select(EmployeeChange)).all() == []


def test_raise_dated_in_the_current_month_is_accepted(client):
    r = _post(client, basic_salary="6000")
    assert r.status_code == 303
    with client.factory() as db:
        assert db.get(Employee, client.emp_id).basic_salary == Decimal("6000.00")


def test_a_non_salary_change_with_an_old_date_is_not_blocked(client):
    old = _months_back(2).replace(day=10).isoformat()
    assert _post(client, position="Supervisor", effective_date=old).status_code == 303


def test_blocked_rule_follows_the_fifth_and_the_run_status():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    nov = date(2031, 11, 1)
    with factory() as db:
        # before the cutoff the previous month is still open; on the 6th it is closed
        assert salary_change_blocked(db, date(2031, 11, 20), date(2031, 12, 5)) is False
        assert salary_change_blocked(db, date(2031, 11, 20), date(2031, 12, 6)) is True
        # an approved run closes it early, a draft does not
        db.add(PayrollRun(period_start=nov, period_end=date(2031, 11, 28), pay_date=date(2031, 11, 28),
                          status="draft", created_by="synthetic"))
        db.commit()
        assert salary_change_blocked(db, date(2031, 11, 20), date(2031, 12, 2)) is False
        run = db.scalar(select(PayrollRun))
        run.status = "approved"
        db.commit()
        assert salary_change_blocked(db, date(2031, 11, 20), date(2031, 12, 2)) is True
        # the current month is never blocked, even with an approved run
        assert salary_change_blocked(db, date(2031, 11, 20), date(2031, 11, 25)) is False
    engine.dispose()


def test_ai_path_refuses_a_future_dated_change_like_the_form(client):
    from datetime import timedelta
    import app.ai_agent as ai
    from app.ai_agent import execute_plan
    ai.SessionLocal = client.factory
    future = (today_cairo() + timedelta(days=90)).isoformat()
    assert _post(client, basic_salary="7000", effective_date=future).status_code == 400
    with pytest.raises(ValueError, match="لا يمكن أن يكون في المستقبل"):
        execute_plan({"action": "edit_employee", "employee_id": client.emp_id, "basic_salary": "7000",
                      "effective_date": future, "reason": "synthetic"}, "tester")
    with client.factory() as db:
        assert db.get(Employee, client.emp_id).basic_salary == Decimal("5000.00")
        assert db.scalars(select(EmployeeChange)).all() == []


def test_ai_multi_field_edit_rolls_back_when_the_salary_block_raises_mid_loop(client):
    from datetime import timedelta
    import app.ai_agent as ai
    from app.ai_agent import execute_plan
    ai.SessionLocal = client.factory
    old = (_months_back(2).replace(day=10)).isoformat()
    with pytest.raises(ValueError):
        execute_plan({"action": "edit_employee", "employee_id": client.emp_id, "position": "Changed",
                      "basic_salary": "8000", "effective_date": old, "reason": "synthetic"}, "tester")
    with client.factory() as db:
        emp = db.get(Employee, client.emp_id)
        assert emp.position == "P" and emp.basic_salary == Decimal("5000.00")
        assert db.scalars(select(EmployeeChange)).all() == []
