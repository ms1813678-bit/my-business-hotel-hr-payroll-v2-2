"""Group 4 real-PostgreSQL flow: one isolated, freshly migrated database per test
(CREATE DATABASE ... alembic head via scripts.initialize_database, dropped in
finally). Real app routes + real services + real PG locks/triggers.
Oracle (salary 6000, approved financial reward 300, paid advance 1000, approved
sanction 200): net 5100.00. Characterization only; no policy claim.
Requires a local PG on 127.0.0.1 reachable as G4_PG_ADMIN_URL (default
postgresql+psycopg://postgres@127.0.0.1:5433/postgres)."""
import os, re, subprocess, sys, uuid
from datetime import date, datetime
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import sessionmaker

ADMIN_URL = os.environ.get("G4_PG_ADMIN_URL", "postgresql+psycopg://postgres@127.0.0.1:5433/postgres")

# Local-only guard: this module CREATEs and DROPs databases, so it refuses any
# admin URL that is not on this computer (CI or a typo cannot touch an external DB).
from sqlalchemy.engine import make_url as _make_url
_host = _make_url(ADMIN_URL).host
if _host not in ("127.0.0.1", "localhost", "::1"):
    pytest.skip("G4_PG_ADMIN_URL must point at a local PostgreSQL (127.0.0.1/localhost/::1)",
                allow_module_level=True)


@pytest.fixture()
def pgapp(monkeypatch):
    name = "g4_" + uuid.uuid4().hex[:12]
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" ENCODING UTF8 TEMPLATE template0'))
    engine = None
    try:
        url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
        env = dict(os.environ, DATABASE_URL=url)
        subprocess.run([sys.executable, "-m", "scripts.initialize_database"], env=env, check=True,
                       capture_output=True)
        engine = create_engine(url)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        from app.db import get_db
        from app.main import app
        from app.models import (AdminAccount, AdvanceInstallment, AdvanceRequest, AttendanceImportBatch,
                                AttendanceMonth, Department, Employee,
                                EmployeeServicePeriod, StaffAction)
        from app.security import hash_password
        ps, pe = date(2026, 10, 1), date(2026, 10, 31)
        now = datetime.utcnow()
        with factory() as db:
            db.add_all([AdminAccount(username="pg-admin", password_hash=hash_password("Payroll-Test-Password-123!"),
                                     active=True)])
            dep = Department(name="PG Dept", active=True); db.add(dep); db.flush()
            emp = Employee(code="MY-PG-0001", full_name="PG Employee", department_id=dep.id,
                           position="p", hire_date=ps, status="active", basic_salary=D("6000.00"),
                           daily_hours=D("8"))
            db.add(emp); db.flush()
            db.add(EmployeeServicePeriod(employee_id=emp.id, start_date=ps, end_date=None,
                                         start_reason="synthetic"))
            b = [AttendanceImportBatch(source_type=t, period_start=ps, period_end=pe,
                                       original_filename=f"{t}.xlsx", file_sha256=ch * 64,
                                       uploaded_by="pg-admin", status="approved", row_count=1,
                                       issue_count=0) for t, ch in (("program_report", "a"), ("fingerprint", "b"))]
            db.add_all(b); db.flush()
            db.add(AttendanceMonth(period_start=ps, period_end=pe, program_import_batch_id=b[0].id,
                                   fingerprint_import_batch_id=b[1].id, status="approved",
                                   created_by="pg-admin", approved_by="pg-admin", approved_at=now,
                                   approval_reason="synthetic"))
            adv = AdvanceRequest(employee_id=emp.id, request_date=date(2026, 9, 16), payroll_month=ps,
                                 amount=D("1000.00"), salary_basis=D("6000.00"), reason="r", status="paid",
                                 created_by="pg-admin", paid_by="pg-admin", paid_at=now)
            db.add(adv); db.flush()
            db.add(AdvanceInstallment(advance_request_id=adv.id, installment_number=1, amount=D("1000.00"),
                                      payroll_month=ps, scheduled_payment_date=date(2026, 11, 5),
                                      status="scheduled", created_at=now))
            for kind, cat, amt in (("reward", "financial_reward", "300.00"), ("sanction", "salary_deduction", "200.00")):
                db.add(StaffAction(employee_id=emp.id, kind=kind, category=cat, title=cat, is_financial=True,
                                   amount=D(amt), payroll_month=ps, reason="r", status="approved",
                                   created_by="pg-admin", approved_by="pg-admin", approved_at=now,
                                   acknowledgement_status="pending"))
            db.commit()

        def override():
            with factory() as db:
                yield db
        app.dependency_overrides[get_db] = override
        monkeypatch.setattr("app.main.SessionLocal", factory)
        try:
            with TestClient(app) as client:
                client.factory = factory
                yield client
        finally:
            app.dependency_overrides.clear()
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _csrf(r):
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text); assert m; return m.group(1)


def _login(c):
    r = c.post("/login", data={"username": "pg-admin", "password": "Payroll-Test-Password-123!",
                               "csrf_token": _csrf(c.get("/login"))}, follow_redirects=False)
    assert r.status_code == 303


def test_pg_full_flow_oracle_5100_close_reversal_and_payment_correction(pgapp):
    from app.models import (AdvanceInstallment, AdvanceRequest, AuditLog, PayrollItem,
                            PayrollPayment, PayrollRun)
    c = pgapp; _login(c)
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "10"}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    with c.factory() as db:
        it = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == run_id))
        item_id = it.id
        assert (it.advance_deduction, it.staff_reward_amount, it.staff_sanction_amount) == (
            D("1000.00"), D("300.00"), D("200.00"))
        assert it.net_pay == D("5100.00")
    for step in ("approve", "close"):
        assert c.post(f"/payroll/runs/{run_id}/{step}", data={"csrf_token": tok},
                      follow_redirects=False).status_code == 303
    with c.factory() as db:
        assert db.get(PayrollRun, run_id).status == "closed"
        assert db.scalar(select(AdvanceRequest.status)) == "deducted"
        assert db.scalar(select(AdvanceInstallment.applied_payroll_run_id)) == run_id
    r = c.post(f"/payroll/items/{item_id}/payments", data={"csrf_token": tok, "amount": "5100.00",
              "paid_date": "2026-11-05", "method": "cash", "reference": "r"}, follow_redirects=False)
    assert "error" not in r.headers["location"]
    with c.factory() as db:
        pid = db.scalar(select(PayrollPayment.id))
    r = c.post(f"/payroll/payments/{pid}/correct", data={"csrf_token": tok, "corrected_amount": "5000.00",
              "reason": "typo"}, follow_redirects=False)
    assert "error" not in r.headers["location"]
    r = c.post(f"/payroll/runs/{run_id}/reopen", data={"csrf_token": tok, "reason": "late fix"},
               follow_redirects=False)
    with c.factory() as db:
        run = db.get(PayrollRun, run_id)
        # record what the current source does when a run with payments is reopened
        assert run.status == "reopened"  # current source allows reopening a run that has payments
        actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
        assert "payroll_payment_corrected" in actions
        pays = db.scalars(select(PayrollPayment).order_by(PayrollPayment.id)).all()
        assert [p.amount for p in pays] == [D("5100.00"), D("5000.00")]
        assert pays[0].superseded_by_payment_id == pays[1].id


def test_pg_close_is_atomic_when_advance_changed_after_draft(pgapp):
    from app.models import AdvanceInstallment, AdvanceRequest, AuditLog, PayrollRun
    c = pgapp; _login(c)
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "10"}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    c.post(f"/payroll/runs/{run_id}/approve", data={"csrf_token": tok})
    with c.factory() as db:                         # late change to the scheduled installment
        db.execute(update(AdvanceInstallment).values(amount=D("900.00"))); db.commit()
    r = c.post(f"/payroll/runs/{run_id}/close", data={"csrf_token": tok}, follow_redirects=False)
    assert "error=close" in r.headers["location"]
    with c.factory() as db:
        assert db.get(PayrollRun, run_id).status == "approved"
        assert db.scalar(select(AdvanceRequest.status)) == "paid"
        assert db.scalar(select(AdvanceInstallment.applied_payroll_run_id)) is None
        assert "payroll_run_closed" not in [a.action for a in db.scalars(select(AuditLog))]
    with c.factory() as db:                         # restore, then close succeeds
        db.execute(update(AdvanceInstallment).values(amount=D("1000.00"))); db.commit()
    c.post(f"/payroll/runs/{run_id}/close", data={"csrf_token": tok})
    with c.factory() as db:
        assert db.get(PayrollRun, run_id).status == "closed"


def test_pg_financial_reward_http_create_approve_enters_payroll(pgapp):
    from app.models import PayrollItem, StaffAction
    c = pgapp; _login(c)
    tok = _csrf(c.get("/staff-actions"))
    emp = 1
    with c.factory() as db:
        from app.models import Employee
        emp = db.scalar(select(Employee.id))
    form = {"csrf_token": tok, "employee_id": str(emp), "kind": "reward", "category": "financial_reward",
            "title": "extra", "is_financial": "yes", "amount": "50.00", "payroll_month": "2026-10-01",
            "reason": "r"}
    r = c.post("/staff-actions", data=form, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    # missing amount for a financial reward is refused
    bad = c.post("/staff-actions", data={**form, "title": "x2", "amount": ""}, follow_redirects=False)
    assert "error" in bad.headers["location"] or bad.status_code == 422
    with c.factory() as db:
        a = db.scalar(select(StaffAction).where(StaffAction.title == "extra"))
        assert (a.status, a.is_financial, a.amount) == ("draft", True, D("50.00"))
        aid = a.id
    assert c.post(f"/staff-actions/{aid}/approve", data={"csrf_token": tok},
                  follow_redirects=False).status_code == 303
    r = c.post("/payroll", data={"csrf_token": _csrf(c.get("/payroll")), "year": "2026", "month": "10"},
               follow_redirects=False)
    with c.factory() as db:
        it = db.scalar(select(PayrollItem))
        assert it.staff_reward_amount == D("350.00")         # seeded 300 + HTTP-created 50
        assert it.net_pay == D("5150.00")                     # 6000 + 350 - 1000 - 200
        assert db.get(StaffAction, aid).payroll_run_id is not None


def test_pg_reopen_without_payment_reverses_advance_installment(pgapp):
    from app.models import AdvanceInstallment, AdvanceRequest, PayrollRun
    c = pgapp; _login(c)
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "10"}, follow_redirects=False)
    run_id = int(r.headers["location"].rsplit("/", 1)[1])
    for step in ("approve", "close"):
        c.post(f"/payroll/runs/{run_id}/{step}", data={"csrf_token": tok})
    with c.factory() as db:
        assert db.scalar(select(AdvanceRequest.status)) == "deducted"
    r = c.post(f"/payroll/runs/{run_id}/reopen", data={"csrf_token": tok, "reason": "recheck"},
               follow_redirects=False)
    assert "error" not in r.headers["location"]
    with c.factory() as db:
        assert db.get(PayrollRun, run_id).status == "reopened"
        adv = db.scalar(select(AdvanceRequest))
        inst = db.scalar(select(AdvanceInstallment))
        assert (adv.status, adv.deducted_at, adv.payroll_run_id) == ("paid", None, None)
        assert inst.status == "reversed" and inst.applied_payroll_run_id is None
        assert inst.reversal_reference and f"payroll-run:{run_id}" in inst.reversal_reference
    # re-approve and re-close consumes the installment exactly once again
    for step in ("approve", "close"):
        c.post(f"/payroll/runs/{run_id}/{step}", data={"csrf_token": tok})
    with c.factory() as db:
        assert db.scalar(select(AdvanceRequest.status)) == "deducted"
        assert db.scalar(select(AdvanceInstallment.applied_payroll_run_id)) == run_id
