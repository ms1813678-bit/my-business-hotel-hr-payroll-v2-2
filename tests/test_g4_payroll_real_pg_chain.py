"""Group 4 / item 9ب subset: chained Oct-Nov-Dec 2026 payroll on real PG, no owner
decision needed. Reuses the per-test migrated DB fixture (local-only guard).
Oct (seed): basic 6000 +300 reward -1000 advance -200 sanction = 5100.00.
Nov: +100 reward (approved), regular advance 500 (paid) => 5600.00.
Dec: nothing => 6000.00. Each month consumes only its own advance and actions;
reopening Oct reverses only Oct and leaves Nov/Dec untouched. Characterization only."""
from datetime import date, datetime
from decimal import Decimal as D

from sqlalchemy import select

from tests.test_g4_payroll_real_pg_flow import _csrf, _login, pgapp  # noqa: F401


def _seed_month(c, ms, me, advance=None, reward=None):
    from app.models import (AdvanceInstallment, AdvanceRequest, AttendanceImportBatch,
                            AttendanceMonth, Employee, StaffAction)
    now = datetime.utcnow()
    with c.factory() as db:
        emp = db.scalar(select(Employee).limit(1)).id
        bs = [AttendanceImportBatch(source_type=t, period_start=ms, period_end=me,
                                    original_filename=f"{t}-{ms}.xlsx", file_sha256=(ch + f"{ms.month:02d}") * 21 + ch,
                                    uploaded_by="pg-admin", status="approved", row_count=1, issue_count=0)
              for t, ch in (("program_report", "c"), ("fingerprint", "d"))]
        db.add_all(bs); db.flush()
        db.add(AttendanceMonth(period_start=ms, period_end=me, program_import_batch_id=bs[0].id,
                               fingerprint_import_batch_id=bs[1].id, status="approved",
                               created_by="pg-admin", approved_by="pg-admin", approved_at=now,
                               approval_reason="synthetic"))
        if advance:
            a = AdvanceRequest(employee_id=emp, request_date=ms.replace(day=16), payroll_month=ms,
                               amount=D(advance), salary_basis=D("6000.00"), reason="r", status="paid",
                               created_by="pg-admin", paid_by="pg-admin", paid_at=now)
            db.add(a); db.flush()
            nm = date(ms.year + (ms.month == 12), ms.month % 12 + 1, 5)
            db.add(AdvanceInstallment(advance_request_id=a.id, installment_number=1, amount=D(advance),
                                      payroll_month=ms, scheduled_payment_date=nm, status="scheduled",
                                      created_at=now))
        if reward:
            db.add(StaffAction(employee_id=emp, kind="reward", category="financial_reward", title=f"r{ms}",
                               is_financial=True, amount=D(reward), payroll_month=ms, reason="r",
                               status="approved", created_by="pg-admin", approved_by="pg-admin",
                               approved_at=now, acknowledgement_status="pending"))
        db.commit()


def _run(c, year, month):
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": str(year), "month": str(month)},
               follow_redirects=False)
    assert "error" not in r.headers["location"], r.headers["location"]
    return int(r.headers["location"].rsplit("/", 1)[1]), tok


def _step(c, run_id, tok, *steps):
    for s in steps:
        assert c.post(f"/payroll/runs/{run_id}/{s}", data={"csrf_token": tok},
                      follow_redirects=False).status_code == 303


def test_pg_three_month_chain_each_month_consumes_only_its_own_sources(pgapp):
    from app.models import AdvanceInstallment, AdvanceRequest, PayrollItem, PayrollRun
    c = pgapp; _login(c)
    _seed_month(c, date(2026, 11, 1), date(2026, 11, 30), advance="500.00", reward="100.00")
    _seed_month(c, date(2026, 12, 1), date(2026, 12, 31))
    oct_id, tok = _run(c, 2026, 10); _step(c, oct_id, tok, "approve", "close")
    nov_id, tok = _run(c, 2026, 11); _step(c, nov_id, tok, "approve", "close")
    dec_id, tok = _run(c, 2026, 12); _step(c, dec_id, tok, "approve", "close")
    with c.factory() as db:
        net = {r.id: db.scalar(select(PayrollItem.net_pay).where(PayrollItem.payroll_run_id == r.id))
               for r in db.scalars(select(PayrollRun))}
        assert (net[oct_id], net[nov_id], net[dec_id]) == (D("5100.00"), D("5600.00"), D("6000.00"))
        applied = {i.payroll_month: i.applied_payroll_run_id for i in db.scalars(select(AdvanceInstallment))}
        assert applied == {date(2026, 10, 1): oct_id, date(2026, 11, 1): nov_id}
        assert {a.status for a in db.scalars(select(AdvanceRequest))} == {"deducted"}
        assert all(r.status == "closed" for r in db.scalars(select(PayrollRun)))


def test_pg_reopen_earlier_month_reverses_only_that_month_and_recloses_once(pgapp):
    from app.models import AdvanceInstallment, PayrollItem, PayrollRun
    c = pgapp; _login(c)
    _seed_month(c, date(2026, 11, 1), date(2026, 11, 30), advance="500.00", reward="100.00")
    oct_id, tok = _run(c, 2026, 10); _step(c, oct_id, tok, "approve", "close")
    nov_id, tok = _run(c, 2026, 11); _step(c, nov_id, tok, "approve", "close")
    r = c.post(f"/payroll/runs/{oct_id}/reopen", data={"csrf_token": tok, "reason": "recheck oct"},
               follow_redirects=False)
    assert "error" not in r.headers["location"]
    with c.factory() as db:
        st = {i.payroll_month: (i.status, i.applied_payroll_run_id) for i in db.scalars(select(AdvanceInstallment))}
        assert st[date(2026, 10, 1)][1] is None and st[date(2026, 10, 1)][0] == "reversed"
        assert st[date(2026, 11, 1)][1] == nov_id                       # Nov untouched
        assert db.get(PayrollRun, nov_id).status == "closed"
        assert db.scalar(select(PayrollItem.net_pay).where(PayrollItem.payroll_run_id == nov_id)) == D("5600.00")
    _step(c, oct_id, tok, "approve", "close")
    with c.factory() as db:
        st = {i.payroll_month: i.applied_payroll_run_id for i in db.scalars(select(AdvanceInstallment))}
        assert st == {date(2026, 10, 1): oct_id, date(2026, 11, 1): nov_id}
        assert db.scalar(select(PayrollItem.net_pay).where(PayrollItem.payroll_run_id == oct_id)) == D("5100.00")


def test_pg_chain_order_rules_current_source(pgapp):
    """Pins what the source does when months are created out of order; no policy."""
    c = pgapp; _login(c)
    _seed_month(c, date(2026, 12, 1), date(2026, 12, 31))
    tok = _csrf(c.get("/payroll"))
    r = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "12"}, follow_redirects=False)
    dec_created_before_oct = "error" not in r.headers["location"]
    r2 = c.post("/payroll", data={"csrf_token": tok, "year": "2026", "month": "12"}, follow_redirects=False)
    assert "error" in r2.headers["location"]          # a second run for the same month is refused
    assert dec_created_before_oct is True             # December may be created while October is still open
