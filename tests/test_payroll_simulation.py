"""What-if payroll simulation: transient copies, advisory output, zero writes."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Department, Employee, PayrollItem, PayrollRun
from app.payroll_simulation import simulate_run_adjustment


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory.begin() as db:
        department = Department(name="الاستقبال", active=True)
        db.add(department)
        db.flush()
        for index in (1, 2):
            db.add(Employee(
                code=f"MY-000{index}", full_name=f"موظف {index}",
                department_id=department.id, position="موظف",
                hire_date=date(2026, 1, 1), basic_salary=Decimal("6000.00"),
                daily_hours=Decimal("8.00"), status="active",
            ))
        db.flush()
        run = PayrollRun(
            period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
            pay_date=date(2026, 12, 1), status="approved", created_by="tester",
        )
        db.add(run)
        db.flush()
        employees = db.scalars(select(Employee).order_by(Employee.id)).all()
        for employee in employees:
            db.add(PayrollItem(
                payroll_run_id=run.id, employee_id=employee.id,
                employee_code_snapshot=employee.code,
                employee_name_snapshot=employee.full_name,
                department_name_snapshot="الاستقبال", position_snapshot="موظف",
                salary_basis=Decimal("6000.00"), base_salary=Decimal("6000.00"),
                daily_hours=Decimal("8.00"),
                attendance_bonus=Decimal("300.00"),
                absence_days=Decimal("0.00"), absence_deduction=Decimal("0.00"),
                insurance_share=Decimal("150.00"),
                net_pay=Decimal("6150.00"),
            ))
        db.commit()
    yield session_factory
    engine.dispose()


def test_add_mode_compares_before_after_without_writing(factory):
    with factory.begin() as db:
        run = db.scalar(select(PayrollRun))
        result = simulate_run_adjustment(
            db, run.id, field="attendance_bonus", mode="add", value="500.00"
        )
        assert result["items_compared"] == 2
        row = result["rows"][0]
        assert row["stored_value"] == "300.00"
        # add mode: 300 stored + 500 = 800
        assert row["hypothetical_value"] == "800.00"
        assert row["net_before"] == "6150.00"
        assert row["net_after"] == "6650.00"
        assert row["net_delta"] == "500.00"
        assert row["feasible"] is True
        assert result["total_net_before"] == "12300.00"
        assert result["total_net_after"] == "13300.00"
    # nothing was written
    with factory() as db:
        item = db.scalar(select(PayrollItem))
        assert Decimal(item.attendance_bonus) == Decimal("300.00")
        assert Decimal(item.net_pay) == Decimal("6150.00")


def test_set_mode_replaces_value(factory):
    with factory.begin() as db:
        run = db.scalar(select(PayrollRun))
        result = simulate_run_adjustment(
            db, run.id, field="attendance_bonus", mode="set", value="1000.00"
        )
        assert result["rows"][0]["hypothetical_value"] == "1000.00"
        assert result["rows"][0]["net_delta"] == "700.00"


def test_infeasible_is_reported_never_clamped(factory):
    with factory.begin() as db:
        run = db.scalar(select(PayrollRun))
        result = simulate_run_adjustment(
            db, run.id, field="other_deductions", mode="add", value="99999.00"
        )
        assert result["infeasible_items"] == 2
        for row in result["rows"]:
            assert row["feasible"] is False
            assert row["net_after"] is None
            assert row["net_delta"] is None
        # totals are withheld as incomplete, never a misleading partial sum
        assert result["totals_complete"] is False
        assert result["total_net_after"] is None
        assert result["total_net_delta"] is None
        assert result["total_net_before"] == "12300.00"
        assert all(row["infeasible_reason"] == "net_negative" for row in result["rows"])


def test_employee_scope_limits_the_comparison(factory):
    with factory.begin() as db:
        run = db.scalar(select(PayrollRun))
        first = db.scalar(select(Employee).order_by(Employee.id))
        result = simulate_run_adjustment(
            db, run.id, field="attendance_bonus", mode="add",
            value="100.00", employee_ids=[first.id],
        )
        assert result["scope"] == "selected_employees"
        assert result["items_compared"] == 1
        assert result["rows"][0]["employee_id"] == first.id


def test_invalid_field_mode_value_are_rejected(factory):
    with factory() as db:
        run = db.scalar(select(PayrollRun))
        with pytest.raises(ValueError, match="field_not_adjustable"):
            simulate_run_adjustment(db, run.id, field="net_pay", mode="add", value="1")
        with pytest.raises(ValueError, match="mode_not_supported"):
            simulate_run_adjustment(db, run.id, field="taxes", mode="multiply", value="1")
        with pytest.raises(ValueError, match="value_not_numeric"):
            simulate_run_adjustment(db, run.id, field="taxes", mode="add", value="abc")
        with pytest.raises(ValueError, match="run_not_found"):
            simulate_run_adjustment(db, 99999, field="taxes", mode="add", value="1")


def test_simulation_route_renders_results_and_stays_read_only(monkeypatch):
    """GET with sim params renders the advisory table; the stored run is
    byte-identical afterwards."""
    import re
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import hash_password
    from app.models import AdminAccount
    from app.db import get_db

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with session_factory.begin() as db:
        db.add(AdminAccount(
            username="sim-admin",
            password_hash=hash_password("Sim-Test-Password-123!"), active=True,
        ))
        department = Department(name="الاستقبال", active=True)
        db.add(department)
        db.flush()
        employee = Employee(
            code="MY-0001", full_name="موظف تجريبي", department_id=department.id,
            position="موظف", hire_date=date(2026, 1, 1),
            basic_salary=Decimal("6000.00"), daily_hours=Decimal("8.00"), status="active",
        )
        db.add(employee)
        db.flush()
        run = PayrollRun(
            period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
            pay_date=date(2026, 12, 1), status="approved", created_by="sim-admin",
        )
        db.add(run)
        db.flush()
        db.add(PayrollItem(
            payroll_run_id=run.id, employee_id=employee.id,
            employee_code_snapshot="MY-0001", employee_name_snapshot="موظف تجريبي",
            department_name_snapshot="الاستقبال", position_snapshot="موظف",
            salary_basis=Decimal("6000.00"), base_salary=Decimal("6000.00"),
            daily_hours=Decimal("8.00"), attendance_bonus=Decimal("300.00"),
            absence_days=Decimal("0.00"), absence_deduction=Decimal("0.00"),
            insurance_share=Decimal("150.00"), net_pay=Decimal("6150.00"),
        ))
        db.flush()
        run_id = run.id

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    try:
        with TestClient(app) as client:
            page = client.get("/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            login = client.post(
                "/login",
                data={"username": "sim-admin", "password": "Sim-Test-Password-123!",
                      "csrf_token": csrf},
                follow_redirects=False,
            )
            assert login.status_code == 303
            response = client.get(
                f"/payroll/runs/{run_id}",
                params={"sim_field": "attendance_bonus", "sim_mode": "add", "sim_value": "500"},
            )
            assert response.status_code == 200
            assert "محاكاة ماذا لو" in response.text
            assert "6650.00" in response.text  # 6150 + 500
            # stored values untouched
            with session_factory() as db:
                item = db.scalar(select(PayrollItem))
                assert Decimal(item.attendance_bonus) == Decimal("300.00")
                assert Decimal(item.net_pay) == Decimal("6150.00")
            # bad params render the advisory error, not a 500
            bad = client.get(
                f"/payroll/runs/{run_id}",
                params={"sim_field": "net_pay", "sim_mode": "add", "sim_value": "1"},
            )
            assert bad.status_code == 200
            assert "تعذر تشغيل المحاكاة" in bad.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _run_id(db):
    return db.scalars(select(PayrollRun)).first().id


def test_invalid_values_are_rejected_before_the_engine(factory):
    with factory.begin() as db:
        run_id = _run_id(db)
        for bad in ("NaN", "Infinity", "-Infinity", "abc"):
            with pytest.raises(ValueError):
                simulate_run_adjustment(db, run_id, field="taxes", mode="add", value=bad)
        with pytest.raises(ValueError, match="value_negative"):
            simulate_run_adjustment(db, run_id, field="taxes", mode="add", value="-1")
        with pytest.raises(ValueError, match="value_too_large"):
            simulate_run_adjustment(db, run_id, field="taxes", mode="set", value="1e999999")
        with pytest.raises(ValueError, match="value_too_large"):
            simulate_run_adjustment(db, run_id, field="taxes", mode="set", value="1000000000")


def test_absence_beyond_month_bounds_reports_the_accurate_reason(factory):
    with factory.begin() as db:
        result = simulate_run_adjustment(
            db, _run_id(db), field="absence_days", mode="set", value="31")
    assert result["infeasible_items"] == len(result["rows"]) > 0
    assert all(row["infeasible_reason"] == "absence_bounds" for row in result["rows"])
    assert result["totals_complete"] is False
    assert result["total_net_after"] is None
    assert result["total_net_delta"] is None


def test_negative_net_reports_its_own_reason(factory):
    with factory.begin() as db:
        result = simulate_run_adjustment(
            db, _run_id(db), field="taxes", mode="set", value="999999")
    assert result["infeasible_items"] == len(result["rows"]) > 0
    assert all(row["infeasible_reason"] == "net_negative" for row in result["rows"])
    assert result["totals_complete"] is False


def test_helper_never_autoflushes_dirty_session_state(factory):
    from sqlalchemy.orm import sessionmaker as _sm
    engine = factory.kw["bind"]
    auto = _sm(bind=engine, autoflush=True, autocommit=False)
    with auto.begin() as db:
        run_id = _run_id(db)
        employee = db.scalars(select(Employee)).first()
        employee.full_name = "اسم متسخ لم يُحفظ"
        result = simulate_run_adjustment(db, run_id, field="taxes", mode="add", value="10")
        assert result["rows"]
        assert employee in db.dirty  # pending change survived: nothing was flushed
