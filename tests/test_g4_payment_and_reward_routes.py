# ruff: noqa: F811
# Imported pytest fixtures intentionally share test parameter names.
"""Group 4 route-level characterization: closed-month payment correction and
reward (staff action) create/approve with CSRF and role gate. Reuses the existing
payroll_client fixture. Characterization only; no policy claim."""
from decimal import Decimal as D

from sqlalchemy import select

from app.models import AdminAccount, AuditLog, PayrollItem, PayrollPayment, StaffAction
from tests.test_payroll_routes import (  # noqa: F401
    create_test_payroll_run, csrf, login, payroll_client)


def _closed_run_with_payment(c, amount="1000.00"):
    login(c)
    run_id = create_test_payroll_run(c)
    tok = csrf(c.get(f"/payroll/runs/{run_id}"))
    c.post(f"/payroll/runs/{run_id}/approve", data={"csrf_token": tok})
    c.post(f"/payroll/runs/{run_id}/close", data={"csrf_token": tok})
    with c.test_session_factory() as db:
        item_id = db.scalar(select(PayrollItem.id).where(PayrollItem.payroll_run_id == run_id))
    r = c.post(f"/payroll/items/{item_id}/payments", data={
        "csrf_token": tok, "amount": amount, "paid_date": "2026-11-05", "method": "cash",
        "reference": "r1"}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    with c.test_session_factory() as db:
        pid = db.scalar(select(PayrollPayment.id))
    return run_id, item_id, pid, tok


def test_payment_correction_route_needs_csrf_and_reason_and_keeps_original(payroll_client):
    c = payroll_client
    run_id, item_id, pid, tok = _closed_run_with_payment(c)
    url = f"/payroll/payments/{pid}/correct"
    assert c.post(url, data={"corrected_amount": "900", "reason": "typo"},
                  follow_redirects=False).status_code == 403            # no CSRF
    r = c.post(url, data={"csrf_token": tok, "corrected_amount": "900", "reason": " "},
               follow_redirects=False)
    assert "error=" in r.headers["location"]                            # blank reason refused
    r = c.post(url, data={"csrf_token": tok, "corrected_amount": "900", "reason": "typo"},
               follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    with c.test_session_factory() as db:
        rows = db.scalars(select(PayrollPayment).order_by(PayrollPayment.id)).all()
        assert [x.amount for x in rows] == [D("1000.00"), D("900.00")]   # original kept
        assert rows[0].superseded_by_payment_id == rows[1].id
        assert rows[1].correction_reason == "typo"
        assert "payroll_payment_corrected" in [a.action for a in db.scalars(select(AuditLog))]


def test_reward_create_approve_routes_csrf_role_and_duplicate(payroll_client):
    c = payroll_client
    login(c)
    emp = c.test_employee_id
    tok = csrf(c.get("/staff-actions"))
    form = {"employee_id": str(emp), "kind": "reward", "category": "commendation",
            "title": "Great work", "reason": "r"}
    assert c.post("/staff-actions", data=form, follow_redirects=False).status_code == 403
    r = c.post("/staff-actions", data={**form, "csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    bad = c.post("/staff-actions", data={**form, "csrf_token": tok, "category": "salary_deduction"},
                 follow_redirects=False)
    assert "error=create" in bad.headers["location"]                    # sanction type under reward
    with c.test_session_factory() as db:
        a = db.scalar(select(StaffAction).where(StaffAction.title == "Great work"))
        aid = a.id
        assert (a.status, a.is_financial) == ("draft", False)
    r = c.post(f"/staff-actions/{aid}/approve", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303
    with c.test_session_factory() as db:
        assert db.get(StaffAction, aid).status == "approved"
    # assistant_manager role is locked out of staff actions
    with c.test_session_factory() as db:
        db.execute(AdminAccount.__table__.update().values(role="assistant_manager")); db.commit()
    c.cookies.clear(); login(c)
    assert c.get("/staff-actions", follow_redirects=False).status_code in (302, 303, 403)
