"""Group 4 (payroll / advances / rewards) characterization at ui-overhaul e60b73f.

Isolated, read-only against shared code: new test module only. Documents what the
current source DOES, with hand-calculated oracles. It does not assert owner policy
where policy is not recorded; those cases are named *_gap_* and only pin behavior.
Run: python -m pytest tests/test_g4_payroll_advance_characterization.py  (or the
bundled stdlib runner at the bottom: python tests/test_g4_...py from repo root).
"""
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.advance_workflow as aw
from app.db import Base
from app.models import Department, Employee, StaffAction
from app.payroll_workflow import recompute_payroll_item


def item(**kw):
    base = dict(
        employee_id=1, salary_basis=D("6000"), base_salary=D("6000"), daily_hours=D("8"),
        performance_bonus=D("600"), attendance_bonus=D("200"), occupancy_bonus=D("0"),
        overtime_hours=D("5"), overtime_approved=True, overtime_amount=D("0"),
        overtime_daily_digest=None, other_bonus=D("100"), staff_reward_amount=D("300"),
        absence_days=D("2"), absence_deduction=D("0"), lateness_deduction=D("50"),
        insurance_share=D("300"), taxes=D("100"), other_deductions=D("25"),
        advance_deduction=D("1500"), staff_sanction_amount=D("200"), net_pay=D("0"))
    base.update(kw)
    return NS(**base)


NOV = NS(period_start=date(2026, 11, 1))   # 30-day month
FEB = NS(period_start=date(2027, 2, 1))    # 28-day month


# ---- 4ت: hand-calculated full month (no DB) --------------------------------
def test_full_month_hand_oracle():
    it = item()
    recompute_payroll_item(it, NOV)
    # absence 6000/30*2 = 400.00 ; OT rate (6000+600)/(30*8)=27.5 -> 5h = 137.50
    assert it.absence_deduction == D("400.00")
    assert it.overtime_amount == D("137.50")
    # gross 6000+200+600+0+137.50+100+300 = 7337.50 ; deductions 400+50+300+100+25+1500+200 = 2575
    assert it.net_pay == D("4762.50")


def test_absence_uses_actual_days_in_month_but_ot_uses_30():
    a, b = item(), item()
    recompute_payroll_item(a, NOV)
    recompute_payroll_item(b, FEB)
    assert a.absence_deduction == D("400.00")
    assert b.absence_deduction == D("428.57")     # 6000/28*2 = 428.571..
    assert a.overtime_amount == b.overtime_amount == D("137.50")


def test_unapproved_overtime_pays_nothing():
    it = item(overtime_approved=False)
    recompute_payroll_item(it, NOV)
    assert it.overtime_amount == D("0.00")
    assert it.net_pay == D("4625.00")             # 4762.50 - 137.50


def test_reward_and_other_bonus_do_not_enter_overtime_basis():
    it = item(staff_reward_amount=D("5000"), other_bonus=D("5000"))
    recompute_payroll_item(it, NOV)
    assert it.overtime_amount == D("137.50")      # basic + performance only


def test_security_12h_divisor_changes_rate_only():
    it = item(daily_hours=D("12"))
    recompute_payroll_item(it, NOV)
    assert it.overtime_amount == D("91.67")       # 6600/360*5 = 91.666..


def test_overtime_ledger_amount_is_not_overwritten():
    it = item(overtime_daily_digest="x" * 64, overtime_amount=D("99.99"))
    recompute_payroll_item(it, NOV)
    assert it.overtime_amount == D("99.99")


def test_deductions_above_earnings_raise_not_clamp():
    # GAP (policy not recorded): the run refuses instead of carrying a shortfall.
    it = item(advance_deduction=D("9000"))
    with pytest.raises(ValueError, match="deductions exceed earnings"):
        recompute_payroll_item(it, NOV)


def test_net_exactly_zero_is_allowed():
    it = item(advance_deduction=D("0"), taxes=D("0"), insurance_share=D("0"), other_deductions=D("0"),
              lateness_deduction=D("0"), staff_sanction_amount=D("0"),
              attendance_bonus=D("0"), performance_bonus=D("0"), other_bonus=D("0"),
              staff_reward_amount=D("0"), overtime_hours=D("0"), absence_days=D("30"))
    recompute_payroll_item(it, NOV)
    assert it.net_pay == D("0.00")


# ---- 4ب: current advance implementation (DB, SQLite stand-in) -----------------
@pytest.fixture
def db(monkeypatch):
    class FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 22)
    monkeypatch.setattr(aw, "date", FakeDate)
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        dep = Department(name="Reception")
        s.add(dep); s.flush()
        s.add(Employee(code="E1", full_name="A", department_id=dep.id, position="p",
                       hire_date=date(2025, 1, 1), basic_salary=D("6000"),
                       daily_hours=D("8"), sync_uid=str(__import__("uuid").uuid4())))
        s.flush()
        yield s


def mk(db, amount, kind="regular", day=20):
    return aw.create_advance(db, employee_id=1, request_date=date(2026, 11, day), amount=D(amount),
                             reason="r", advance_type=kind, actor="hr")


def test_cap_is_25_percent_of_basic_exactly(db):
    mk(db, "1500.00")                              # 6000*25% = 1500 allowed
    with pytest.raises(ValueError, match="Monthly advance limit is 1500.00"):
        mk(db, "0.01")


def test_cap_over_by_one_cent_rejected(db):
    with pytest.raises(ValueError):
        mk(db, "1500.01")


def test_cap_is_cumulative_per_month(db):
    mk(db, "1000"); mk(db, "500")
    with pytest.raises(ValueError, match="already reserved: 1500.00"):
        mk(db, "1")


def test_rejected_advance_releases_reservation(db):
    r = mk(db, "1500")
    aw.reject_advance(db, r, "hr", "no")
    mk(db, "1500")


def test_window_day_15_to_22_only(db):
    for d in (14, 23):
        with pytest.raises(ValueError, match="day 15 through day 22"):
            mk(db, "10", day=d)
    mk(db, "10", day=15); mk(db, "10", day=22)


def test_regular_approval_must_be_single_deduction(db):
    r = mk(db, "100")
    with pytest.raises(ValueError, match="one monthly advance"):
        aw.approve_advance(db, r, "hr", installment_count=3)
    aw.approve_advance(db, r, "hr")
    assert r.status == "approved"


def test_approval_cannot_skip_pending_and_paid_needs_approval(db):
    r = mk(db, "100")
    with pytest.raises(ValueError):
        aw.mark_advance_paid(db, r, "hr")
    aw.approve_advance(db, r, "hr")
    with pytest.raises(ValueError):
        aw.approve_advance(db, r, "hr")


def test_idempotent_submission_key_returns_same_request(db):
    k = "k" * 40
    a = aw.create_advance(db, employee_id=1, request_date=date(2026, 11, 20), amount=D("100"),
                          reason="r", advance_type="regular", actor="hr", submission_key=k)
    b = aw.create_advance(db, employee_id=1, request_date=date(2026, 11, 20), amount=D("100"),
                          reason="r", advance_type="regular", actor="hr", submission_key=k)
    assert a.id == b.id
    with pytest.raises(ValueError, match="different advance"):
        aw.create_advance(db, employee_id=1, request_date=date(2026, 11, 20), amount=D("101"),
                          reason="r", advance_type="regular", actor="hr", submission_key=k)


def test_current_installment_type_exists_and_is_not_capped(db):
    """Characterize current installment handling only. Numeric terms and exemptions are not owner policy proof."""
    r = mk(db, "5000", kind="installment")         # > 25% of 6000, accepted
    aw.approve_advance(db, r, "hr", installment_count=4)
    from sqlalchemy import select
    from app.models import AdvanceInstallment
    rows = db.scalars(select(AdvanceInstallment).order_by(AdvanceInstallment.installment_number)).all()
    assert [x.amount for x in rows] == [D("1250.00")] * 4
    assert [x.payroll_month for x in rows] == [date(2026, 11, 1), date(2026, 12, 1),
                                               date(2027, 1, 1), date(2027, 2, 1)]


def test_installment_remainder_cents_front_loaded():
    from app.advance_repayment_schedule import build_repayment_schedule as b
    s = b(D("100.00"), date(2026, 11, 20), 3)
    assert [x["amount"] for x in s] == [D("33.34"), D("33.33"), D("33.33")]
    assert sum(x["amount"] for x in s) == D("100.00")
    assert s[0]["pay_date"] == date(2026, 12, 5)


# ---- rewards: model-level facts ---------------------------------------------
def test_staff_action_financial_fields_constraint(db):
    from sqlalchemy.exc import IntegrityError
    db.add(StaffAction(employee_id=1, kind="reward", category="x", title="t", reason="r",
                       is_financial=True, amount=None, created_by="hr"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
