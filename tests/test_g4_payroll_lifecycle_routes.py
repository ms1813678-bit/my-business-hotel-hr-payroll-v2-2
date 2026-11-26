# ruff: noqa: F811
# Imported pytest fixtures intentionally share test parameter names.
"""Group 4A route-level characterization: payroll approve / close / reopen.

Reuses the existing payroll_client fixture (SQLite stand-in for the app DB; the
session only requires the PG migrated DB to exist). Checks role gate, CSRF and the
three-step chain through the real routes. Characterization only; no policy claim.
"""
from sqlalchemy import select

from app.models import AdminAccount, AuditLog, PayrollRun
from tests.test_payroll_routes import (  # noqa: F401  (fixture re-export)
    create_test_payroll_run, csrf, login, payroll_client)


def _run_status(client, run_id):
    with client.test_session_factory() as db:
        return db.get(PayrollRun, run_id).status


def _actions(client):
    with client.test_session_factory() as db:
        return [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))
                if a.action.startswith("payroll_run_")]


def test_unauthenticated_and_missing_csrf_cannot_approve_close_reopen(payroll_client):
    c = payroll_client
    for path in ("approve", "close", "reopen"):
        assert c.post(f"/payroll/runs/1/{path}", data={}, follow_redirects=False).status_code in (401, 403)
    login(c)
    run_id = create_test_payroll_run(c)
    for path in ("approve", "close"):
        r = c.post(f"/payroll/runs/{run_id}/{path}", data={}, follow_redirects=False)
        assert r.status_code == 403
    assert _run_status(c, run_id) == "draft"


def test_assistant_manager_role_cannot_reach_payroll_routes(payroll_client):
    c = payroll_client
    login(c)
    run_id = create_test_payroll_run(c)
    with c.test_session_factory() as db:
        db.execute(AdminAccount.__table__.update().values(role="assistant_manager"))
        db.commit()
    c.cookies.clear()
    login(c)
    r = c.get(f"/payroll/runs/{run_id}", follow_redirects=False)
    assert r.status_code in (302, 303, 403)
    r = c.post(f"/payroll/runs/{run_id}/approve", data={"csrf_token": "x"}, follow_redirects=False)
    assert r.status_code in (302, 303, 403)
    assert _run_status(c, run_id) == "draft"


def test_route_chain_approve_then_close_then_reopen_needs_reason(payroll_client):
    c = payroll_client
    login(c)
    run_id = create_test_payroll_run(c)
    page = c.get(f"/payroll/runs/{run_id}")
    tok = csrf(page)
    # close before approve is refused
    r = c.post(f"/payroll/runs/{run_id}/close", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and "error=close" in r.headers["location"]
    r = c.post(f"/payroll/runs/{run_id}/approve", data={"csrf_token": tok}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert _run_status(c, run_id) == "approved"           # approve alone does not close
    r = c.post(f"/payroll/runs/{run_id}/close", data={"csrf_token": tok}, follow_redirects=False)
    assert _run_status(c, run_id) == "closed"
    # reopen with blank reason: refused, status unchanged
    r = c.post(f"/payroll/runs/{run_id}/reopen", data={"csrf_token": tok, "reason": "  "},
               follow_redirects=False)
    assert "error=reopen" in r.headers["location"] and _run_status(c, run_id) == "closed"
    r = c.post(f"/payroll/runs/{run_id}/reopen", data={"csrf_token": tok, "reason": "tax fix"},
               follow_redirects=False)
    assert _run_status(c, run_id) == "reopened"
    assert _actions(c) == ["payroll_run_created", "payroll_run_approved", "payroll_run_closed", "payroll_run_reopened"]
