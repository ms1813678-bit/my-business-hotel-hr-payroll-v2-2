"""Owner decision (Mo, 2026-11-26 16:32 'agree'): a later month cannot be created while an earlier run is open.
Real PG. Reuses the Group 4 per-test migrated DB fixture."""
from datetime import date
from sqlalchemy import select
from tests.test_g4_payroll_real_pg_flow import _csrf, _login, pgapp  # noqa: F401
from tests.test_g4_payroll_real_pg_chain import _seed_month, _run, _step


def test_later_month_refused_while_earlier_run_open_then_allowed_after_close(pgapp):
    from app.models import PayrollRun
    c = pgapp; _login(c)
    _seed_month(c, date(2026, 12, 1), date(2026, 12, 31))
    oct_id, tok = _run(c, 2026, 10)                       # October exists, still draft
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "12"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    with c.factory() as db:
        assert [x.period_start for x in db.scalars(select(PayrollRun))] == [date(2026, 10, 1)]
    _step(c, oct_id, tok, "approve")                      # approved is still open
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "12"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    _step(c, oct_id, tok, "close")
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "12"}, follow_redirects=False)
    assert "error" not in r.headers["location"]
    r = c.post(f"/payroll/runs/{oct_id}/reopen", data={"csrf_token": tok, "reason": "recheck"}, follow_redirects=False)
    assert "error" not in r.headers["location"]
