"""Carry of excess deductions on real PG (migration 0081). Design relayed via Main (this docstring is not an
approval record). Oct normal (5100.00). Nov: installment 7000 on basic 6000 => net 0.00, carried_out 1000.00.
Dec: carried_in 1000.00 shown as its own deduction => net 5000.00. Reopening Nov while Dec exists is refused.
Real PG, py3.11."""
import shutil
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from tests.test_g4_payroll_real_pg_chain import _run, _seed_month, _step
from tests.test_g4_payroll_real_pg_flow import _csrf, _login, pgapp  # noqa: F401


def _chain(c):
    _seed_month(c, date(2026, 11, 1), date(2026, 11, 30), advance="7000.00")
    _seed_month(c, date(2026, 12, 1), date(2026, 12, 31))
    oct_id, tok = _run(c, 2026, 10); _step(c, oct_id, tok, "approve", "close")
    nov_id, tok = _run(c, 2026, 11)
    return oct_id, nov_id, tok


def test_excess_deduction_gives_zero_net_and_carries_to_next_month(pgapp):
    from app.models import PayrollItem
    c = pgapp; _login(c)
    oct_id, nov_id, tok = _chain(c)
    with c.factory() as db:
        it = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == nov_id))
        assert (it.net_pay, it.carried_out_amount, it.carried_in_deduction) == (D("0.00"), D("1000.00"), D("0.00"))
    _step(c, nov_id, tok, "approve", "close")
    dec_id, tok = _run(c, 2026, 12)
    with c.factory() as db:
        it = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == dec_id))
        assert (it.carried_in_deduction, it.carried_out_amount, it.net_pay) == (D("1000.00"), D("0.00"), D("5000.00"))
        oct_item = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == oct_id))
        assert (oct_item.carried_in_deduction, oct_item.carried_out_amount) == (D("0.00"), D("0.00"))
    _step(c, dec_id, tok, "approve", "close")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="UNRAN: pdftotext (poppler) not installed on this runner")
def test_carry_is_a_separate_line_on_the_next_payslip(pgapp):
    import subprocess, tempfile
    from app.models import PayrollItem
    c = pgapp; _login(c)
    oct_id, nov_id, tok = _chain(c)
    _step(c, nov_id, tok, "approve", "close")
    dec_id, tok = _run(c, 2026, 12)
    with c.factory() as db:
        item_id = db.scalar(select(PayrollItem.id).where(PayrollItem.payroll_run_id == dec_id))
    def pdf_text(run_id, iid):
        r = c.get(f"/payroll/runs/{run_id}/items/{iid}/payslip.pdf")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(r.content); f.flush()
            return subprocess.run(["pdftotext", f.name, "-"], capture_output=True, text=True).stdout
    dec = pdf_text(dec_id, item_id)
    assert "1000" in dec.replace(",", "").replace("\u066c", "")      # the carried amount is printed


def test_reopening_month_that_carries_into_existing_later_month_is_refused(pgapp):
    from app.models import PayrollRun
    c = pgapp; _login(c)
    oct_id, nov_id, tok = _chain(c)
    _step(c, nov_id, tok, "approve", "close")
    dec_id, tok = _run(c, 2026, 12)
    r = c.post(f"/payroll/runs/{nov_id}/reopen", data={"csrf_token": tok, "reason": "recheck nov"},
               follow_redirects=False)
    assert "error" in r.headers["location"]
    with c.factory() as db:
        assert db.get(PayrollRun, nov_id).status == "closed"


def test_negative_carry_is_rejected_by_the_database(pgapp):
    c = pgapp; _login(c)
    oct_id, nov_id, tok = _chain(c)
    with c.factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE payroll_items SET carried_in_deduction = -1 WHERE payroll_run_id = :r"), {"r": nov_id})
            db.flush()
        db.rollback()


def test_approve_refuses_a_changed_carry_when_a_later_month_exists(pgapp):
    """Defense in depth: reopen is already refused, so force the state directly and approve must still refuse."""
    from app.models import PayrollItem, PayrollRun
    c = pgapp; _login(c)
    oct_id, nov_id, tok = _chain(c)
    _step(c, nov_id, tok, "approve", "close")
    dec_id, tok = _run(c, 2026, 12)
    with c.factory() as db:
        db.get(PayrollRun, nov_id).status = "reopened"
        it = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == nov_id))
        it.other_deductions = D("500.00")                  # carried_out would become 1500.00, Dec still holds 1000.00
        db.commit()
    r = c.post(f"/payroll/runs/{nov_id}/approve", data={"csrf_token": tok}, follow_redirects=False)
    assert "error" in r.headers["location"]
    with c.factory() as db:
        assert db.get(PayrollRun, nov_id).status == "reopened"


def test_excel_register_foots_gross_minus_deductions_to_net_minus_carry(pgapp):
    from io import BytesIO
    from openpyxl import load_workbook
    from app.reports import build_hr_workbook
    c = pgapp; _login(c)
    oct_id, nov_id, tok = _chain(c)
    _step(c, nov_id, tok, "approve", "close")
    _run(c, 2026, 12)
    with c.factory() as db:
        wb = load_workbook(BytesIO(build_hr_workbook(db, date(2026, 10, 1), date(2026, 12, 31))), data_only=True)
    sheet = wb["Payroll"]
    h = [x.value for x in sheet[1]]
    rows = [dict(zip(h, [x.value for x in r])) for r in sheet.iter_rows(min_row=2)]
    assert len(rows) == 3
    gain = ["Base after proration", "Attendance bonus", "Performance bonus", "Occupancy bonus", "Overtime",
            "Other bonus", "Financial reward", "Leave cash payout"]
    loss = ["Absence deduction", "Lateness deduction", "Insurance", "Taxes", "Other deductions", "Advance",
            "Financial sanction", "Carried in deduction"]
    for r in rows:
        assert round(sum(r[k] for k in gain) - sum(r[k] for k in loss), 2) == round(r["Net pay"] - r["Carried out (unabsorbed)"], 2)
    assert sorted(r["Carried out (unabsorbed)"] for r in rows) == [0, 0, 1000.0]


def test_carry_and_leave_cash_labels_have_english_and_chinese_entries():
    from app.i18n import EN, ZH
    for ar in ("مرحّل من الشهر السابق", "بدل رصيد الإجازات"):
        assert EN[ar] and ZH[ar] and EN[ar] != ar and ZH[ar] != ar
        assert not any("\u0600" <= ch <= "\u06ff" for ch in EN[ar] + ZH[ar])
