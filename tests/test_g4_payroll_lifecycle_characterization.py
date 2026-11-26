"""Group 4A: payroll run approve / close / reopen / closed-month correction.

Characterization of the source at ui-overhaul e60b73f (SQLite stand-in). New test
module only. Records what the code does; it does not assert owner policy.
Route-level facts (read from app/main.py, NOT executed here): approve, close and
reopen are three separate POST routes, each CSRF-protected and gated by
require_payroll_access; reopen takes a required reason. No second confirmation
factor (password/typed phrase) exists beyond CSRF: pinned as a question, not a bug.
"""
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (EmployeeServicePeriod, AttendanceMonth, AuditLog, Department, Employee, PayrollItem,
                        PayrollPayment, PayrollRun)
import app.payroll_workflow as pw
import app.staff_actions_workflow as sa


@pytest.fixture
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        dep = Department(name="Reception")
        s.add(dep); s.flush()
        s.add(Employee(code="E1", full_name="A", department_id=dep.id, position="p",
                       hire_date=date(2025, 1, 1), basic_salary=D("6000"), daily_hours=D("8"),
                       sync_uid=str(__import__("uuid").uuid4())))
        s.add(AttendanceMonth(period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
                              status="approved", created_by="hr",
                              program_import_batch_id=1, fingerprint_import_batch_id=1))
        s.flush()
        yield s


def make_run(db, status="draft"):
    run = PayrollRun(period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
                     pay_date=date(2026, 12, 5), status=status, created_by="hr")
    db.add(run); db.flush()
    it = PayrollItem(payroll_run_id=run.id, employee_id=1, employee_code_snapshot="E1",
                     employee_name_snapshot="A", department_name_snapshot="Reception",
                     position_snapshot="p", salary_basis=D("6000"), base_salary=D("6000"),
                     daily_hours=D("8"))
    db.add(it); db.flush()
    return run, it


def audit_actions(db):
    return [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]


def test_close_requires_prior_approval(db):
    run, _ = make_run(db)
    with pytest.raises(ValueError, match="Only approved payroll can be closed"):
        pw.close_payroll_run(db, run, "hr")


def test_approve_computes_net_and_audits_then_close_is_separate_step(db):
    run, it = make_run(db)
    pw.approve_payroll_run(db, run, "hr")
    assert (run.status, run.approved_by) == ("approved", "hr")
    assert it.net_pay == D("6000.00")
    assert run.closed_at is None                      # approve does not close
    pw.close_payroll_run(db, run, "fin")
    assert (run.status, run.closed_by) == ("closed", "fin")
    assert audit_actions(db) == ["payroll_run_approved", "payroll_run_closed"]


def test_cannot_approve_twice_or_without_attendance_approval(db):
    run, _ = make_run(db)
    pw.approve_payroll_run(db, run, "hr")
    with pytest.raises(ValueError, match="draft or reopened"):
        pw.approve_payroll_run(db, run, "hr")
    db.execute(AttendanceMonth.__table__.update().values(status="under_review"))

    run.status = "reopened"                      # force a re-approval attempt
    with pytest.raises(ValueError, match="Attendance approval is required"):
        pw.approve_payroll_run(db, run, "hr")


def test_run_without_items_cannot_be_approved(db):
    run = PayrollRun(period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
                     pay_date=date(2026, 12, 5), status="draft", created_by="hr")
    db.add(run); db.flush()
    with pytest.raises(ValueError, match="without employee items"):
        pw.approve_payroll_run(db, run, "hr")


def test_item_edit_blocked_once_approved_or_closed(db):
    run, it = make_run(db)
    pw.update_payroll_item(db, it, {"taxes": D("10")}, "hr")
    pw.approve_payroll_run(db, run, "hr")
    with pytest.raises(ValueError, match="only while the run is draft or reopened"):
        pw.update_payroll_item(db, it, {"taxes": D("20")}, "hr")


def test_item_edit_rejects_non_manual_fields(db):
    run, it = make_run(db)
    for field in ("net_pay", "advance_deduction", "staff_reward_amount", "base_salary"):
        with pytest.raises(ValueError, match="cannot be edited"):
            pw.update_payroll_item(db, it, {field: D("1")}, "hr")


def test_reopen_needs_closed_run_and_reason_and_keeps_audit_trail(db):
    run, it = make_run(db)
    pw.approve_payroll_run(db, run, "hr")
    with pytest.raises(ValueError, match="Only a closed"):
        pw.reopen_payroll_run(db, run, "hr", "why")
    pw.close_payroll_run(db, run, "hr")
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="reason is required"):
            pw.reopen_payroll_run(db, run, "hr", bad)
    assert run.status == "closed"
    pw.reopen_payroll_run(db, run, "hr", "  wrong tax  ")
    assert (run.status, run.reopen_reason, run.reopened_by) == ("reopened", "wrong tax", "hr")
    assert audit_actions(db)[-1] == "payroll_run_reopened"
    # earlier approval/close rows are still there (nothing rewritten)
    assert audit_actions(db)[:2] == ["payroll_run_approved", "payroll_run_closed"]


def test_reopened_run_can_be_edited_and_reapproved(db):
    run, it = make_run(db)
    pw.approve_payroll_run(db, run, "hr"); pw.close_payroll_run(db, run, "hr")
    pw.reopen_payroll_run(db, run, "hr", "fix tax")
    pw.update_payroll_item(db, it, {"taxes": D("100")}, "hr")
    pw.approve_payroll_run(db, run, "hr")
    assert it.net_pay == D("5900.00")


def test_payment_only_after_close_and_not_above_net(db):
    run, it = make_run(db)
    pw.approve_payroll_run(db, run, "hr")
    with pytest.raises(ValueError, match="only after payroll is closed"):
        pw.add_payroll_payment(db, it, amount=D("1"), paid_date=date(2026, 12, 5),
                               method="cash", reference=None, actor="fin")
    pw.close_payroll_run(db, run, "hr")
    pw.add_payroll_payment(db, it, amount=D("6000"), paid_date=date(2026, 12, 5),
                           method="cash", reference=None, actor="fin")
    with pytest.raises(ValueError, match="exceeds the remaining"):
        pw.add_payroll_payment(db, it, amount=D("0.01"), paid_date=date(2026, 12, 5),
                               method="cash", reference=None, actor="fin")


def test_payment_correction_supersedes_without_deleting_original(db):
    run, it = make_run(db)
    pw.approve_payroll_run(db, run, "hr"); pw.close_payroll_run(db, run, "hr")
    p = pw.add_payroll_payment(db, it, amount=D("1000"), paid_date=date(2026, 12, 5),
                               method="cash", reference="r", actor="fin")
    with pytest.raises(ValueError, match="reason is required"):
        pw.correct_payroll_payment(db, p, corrected_amount=D("900"), reason=" ", actor="fin")
    with pytest.raises(ValueError, match="nothing to correct"):
        pw.correct_payroll_payment(db, p, corrected_amount=D("1000"), reason="x", actor="fin")
    c = pw.correct_payroll_payment(db, p, corrected_amount=D("900"), reason="typo", actor="fin")
    assert p.superseded_by_payment_id == c.id and c.correction_of_id == p.id
    assert db.get(PayrollPayment, p.id).amount == D("1000.00")      # original kept
    assert pw.payment_totals(db, it)[0] == D("900.00")
    with pytest.raises(ValueError, match="already corrected"):
        pw.correct_payroll_payment(db, p, corrected_amount=D("800"), reason="again", actor="fin")
    assert audit_actions(db)[-1] == "payroll_payment_corrected"


def test_paid_item_cannot_be_edited_after_reopen(db):
    run, it = make_run(db)
    pw.approve_payroll_run(db, run, "hr"); pw.close_payroll_run(db, run, "hr")
    pw.add_payroll_payment(db, it, amount=D("100"), paid_date=date(2026, 12, 5),
                           method="cash", reference=None, actor="fin")
    pw.reopen_payroll_run(db, run, "hr", "fix")
    with pytest.raises(ValueError, match="recorded payments cannot be edited"):
        pw.update_payroll_item(db, it, {"taxes": D("5")}, "hr")


# ---- named reward / sanction types (current source values, no policy claim) ----
def test_reward_category_values_current_source():
    assert sa.REWARD_CATEGORIES == {"financial_reward", "certificate", "commendation",
                                    "promotion_recommendation", "custom"}
    assert sa.SANCTION_CATEGORIES == {"verbal_notice", "first_warning", "final_warning",
                                      "salary_deduction", "suspension", "custom"}


def mk_action(db, **kw):
    base = dict(employee_id=1, kind="reward", category="financial_reward", title="t",
                is_financial=True, amount=D("100"), payroll_month=date(2026, 11, 1),
                reason="r", actor="hr")
    base.update(kw)
    return sa.create_staff_action(db, **base)


def test_reward_validation_rules(db):
    with pytest.raises(ValueError, match="Invalid category"):
        mk_action(db, category="salary_deduction")            # sanction type under reward
    with pytest.raises(ValueError, match="financial amount"):
        mk_action(db, is_financial=False, amount=None, payroll_month=None)
    with pytest.raises(ValueError, match="first day"):
        mk_action(db, payroll_month=date(2026, 11, 2))
    with pytest.raises(ValueError, match="positive"):
        mk_action(db, amount=D("0"))
    mk_action(db, category="certificate", is_financial=False, amount=None, payroll_month=None)


def test_duplicate_financial_reward_blocked_and_approval_audited(db):
    a = mk_action(db)
    with pytest.raises(ValueError, match="identical active"):
        mk_action(db)
    sa.approve_staff_action(db, a, "fin")
    assert a.status == "approved"
    with pytest.raises(ValueError, match="Only draft"):
        sa.approve_staff_action(db, a, "fin")
    assert audit_actions(db) == ["reward_created", "reward_approved"]


def test_create_payroll_run_picks_up_only_approved_financial_actions(db):
    ok = mk_action(db, title="ok"); sa.approve_staff_action(db, ok, "fin")
    mk_action(db, title="draft", amount=D("50"))                   # stays draft
    sanc = mk_action(db, kind="sanction", category="salary_deduction", title="s", amount=D("30"))
    sa.approve_staff_action(db, sanc, "fin")
    db.add(EmployeeServicePeriod(employee_id=1, start_date=date(2025, 1, 1))); db.flush()
    run = pw.create_payroll_run(db, year=2026, month=11, actor="hr")
    it = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == run.id))
    assert it.staff_reward_amount == D("100.00")      # draft 50 not included
    assert it.staff_sanction_amount == D("30.00")


def _created_run(db):
    db.add(EmployeeServicePeriod(employee_id=1, start_date=date(2025, 1, 1))); db.flush()
    run = pw.create_payroll_run(db, year=2026, month=11, actor="hr")
    return run, db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == run.id))


def test_created_run_full_chain_without_overtime(db):
    run, it = _created_run(db)
    assert run.overtime_daily_required is True and it.net_pay == D("6000.00")
    pw.approve_payroll_run(db, run, "hr"); pw.close_payroll_run(db, run, "hr")
    assert run.status == "closed"


def test_created_run_refuses_manual_monthly_overtime_hours_at_edit(db):
    """Current source: on a run from create_payroll_run, typed monthly overtime
    hours are refused at edit time (daily review required). Behavior pin only."""
    run, it = _created_run(db)
    with pytest.raises(ValueError, match="explicit day replacement"):
        pw.update_payroll_item(db, it, {"overtime_hours": D("3")}, "hr")


def test_hand_month_with_paid_advance_reward_sanction_close_and_reopen(db, monkeypatch):
    """4ت oracle: salary 6000 + approved reward 300 - paid advance 1000 - approved
    sanction 200 = 5100.00; close marks the advance deducted; reopen reverses it."""
    import app.advance_workflow as aw
    from app.models import AdvanceInstallment

    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 20)
    monkeypatch.setattr(aw, "date", FakeDate)
    adv = aw.create_advance(db, employee_id=1, request_date=date(2026, 11, 20), amount=D("1000"),
                            reason="r", advance_type="regular", actor="hr")
    aw.approve_advance(db, adv, "hr"); aw.mark_advance_paid(db, adv, "hr")
    r = mk_action(db, amount=D("300")); sa.approve_staff_action(db, r, "fin")
    s_ = mk_action(db, kind="sanction", category="salary_deduction", title="s", amount=D("200"))
    sa.approve_staff_action(db, s_, "fin")
    run, it = _created_run(db)
    assert (it.advance_deduction, it.staff_reward_amount, it.staff_sanction_amount) == (
        D("1000.00"), D("300.00"), D("200.00"))
    assert it.net_pay == D("5100.00")
    pw.approve_payroll_run(db, run, "hr"); pw.close_payroll_run(db, run, "hr")
    assert adv.status == "deducted"
    inst = db.scalar(select(AdvanceInstallment))
    assert inst.applied_payroll_run_id == run.id
    pw.reopen_payroll_run(db, run, "hr", "check")
    assert adv.status == "paid" and inst.status == "reversed"
    assert it.net_pay == D("5100.00")        # snapshot not silently recomputed on reopen


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
