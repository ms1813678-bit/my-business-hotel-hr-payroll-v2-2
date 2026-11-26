"""PACK D / 2z extra pins: where the settlement-year, sick-leave-year, old monthly
overtime and performance-bonus inputs come from, at source 778196c.

NEW tests only. No core change. unittest style so it runs with
`python -m unittest` or pytest. Pure modules are imported for real. Modules that
need pydantic/fastapi (not installed in the sandbox) are read as text/AST; those
tests are marked STATIC and pin the source text, not runtime behaviour.
Set REPO_ROOT to the extracted tree (default: two levels above this file).
"""
import ast
import os
import sys
import unittest
from datetime import date
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

ROOT = os.environ.get('REPO_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class SettlementYearSource(unittest.TestCase):
    """SOURCE 1: settlement year = caller-supplied integer source_year.
    File: app/leave_annual_settlement_source.py::review_annual_settlement (selection),
    app/leave_annual_settlement.py::annual_settlement_split (pure split)."""

    def split(self, **kw):
        from app.leave_annual_settlement import annual_settlement_split
        base = dict(full_annual_entitlement=21, remaining=Fraction(10), hire_date=date(2020, 1, 1),
                    source_year=2025, other_half_action='carry', annual_release_proven=True)
        base.update(kw)
        return annual_settlement_split(**base)

    def test_cash_is_half_of_entitlement_capped_by_remaining(self):
        r = self.split(remaining=Fraction(30))
        self.assertEqual(r.cash_days, Fraction(21, 2))
        self.assertEqual(r.carried_days, Fraction(30) - Fraction(21, 2))
        r = self.split(remaining=Fraction(4))
        self.assertEqual((r.cash_days, r.carried_days), (Fraction(4), Fraction(0)))

    def test_zero_action_clears_instead_of_carry(self):
        r = self.split(remaining=Fraction(30), other_half_action='zero')
        self.assertEqual((r.carried_days, r.cleared_days), (Fraction(0), Fraction(39, 2)))

    def test_source_year_must_be_plain_int_in_range(self):
        for bad in (True, '2025', 0, 10000, None):
            with self.assertRaises(ValueError):
                self.split(source_year=bad)

    def test_hire_after_source_year_end_refused(self):
        with self.assertRaises(ValueError):
            self.split(hire_date=date(2026, 1, 1), source_year=2025)

    def test_entitlement_only_15_21_30(self):
        for bad in (14, 20, 22, 31, True):
            with self.assertRaises(ValueError):
                self.split(full_annual_entitlement=bad)

    def test_late_year_hire_in_source_year_nov_dec_is_preserved_not_cashed(self):
        for month in (11, 12):
            r = self.split(hire_date=date(2025, month, 5))
            self.assertTrue(r.late_year_preservation)
            self.assertEqual(r.cash_days, 0)
            self.assertEqual(r.carried_days, Fraction(10))
            with self.assertRaises(ValueError):
                self.split(hire_date=date(2025, month, 5), other_half_action='zero')
        self.assertFalse(self.split(hire_date=date(2025, 10, 31)).late_year_preservation)
        # Nov hire in a DIFFERENT year than source_year is not late-year.
        self.assertFalse(self.split(hire_date=date(2024, 11, 5)).late_year_preservation)

    def test_unreleased_remainder_not_cash_eligible(self):
        with self.assertRaises(ValueError):
            self.split(annual_release_proven=False)

    def test_zero_or_debt_requires_carry(self):
        r = self.split(remaining=Fraction(-2))
        self.assertEqual((r.cash_days, r.carried_days), (0, Fraction(-2)))
        with self.assertRaises(ValueError):
            self.split(remaining=Fraction(0), other_half_action='zero')

    def test_static_year_selection_dispatch(self):
        """STATIC: year2 builder used when opening baseline year < source_year; else completed-year source.
        Policy must be calendar; full entitlement must equal one earned tier."""
        src = _read('app/leave_annual_settlement_source.py')
        self.assertIn("baseline.year < source_selection['source_year']", src)
        self.assertIn('build_year2_recorded_source', src)
        self.assertIn('review_completed_year_source', src)
        self.assertIn("ledger_delta='0/1'", src)
        self.assertIn('posting_allowed=False', src)
        cy = _read('app/leave_completed_year_source.py')
        self.assertIn("policy.annual_year_basis != 'calendar'", cy)
        self.assertIn('reviewed_on < date(source_year + 1, 1, 1)', cy)
        self.assertIn('baseline.year != source_year', cy)


class SickLeaveYearSource(unittest.TestCase):
    """SOURCE 2: NO dedicated sick-leave-year source exists. Sick days enter only as the
    monthly count `counts['S']` in app/leave_month_earning.py and the policy flags in
    app/leave_policy_preview.py (annual_deduct_sick, holiday_credit_during_sick,
    sick_alert_thresholds). Nothing in app/ defines a sick year window or applies the
    alert thresholds (OWNER QUESTION)."""

    def policy(self, **kw):
        from app.leave_policy_preview import LeavePolicy
        base = dict(tier30_by_insurance_years=True, tier30_by_age_50=True, annual_deduct_unpaid_absence=True,
                    annual_deduct_sick=False, annual_year_basis='calendar', cash_basis='prorated',
                    holiday_credit_during_sick=False, sick_alert_thresholds=(90, 180))
        base.update(kw)
        return LeavePolicy(**base)

    def test_sick_days_deduct_from_annual_only_when_flag_true(self):
        from app.leave_policy_preview import annual_deductible_count as f
        self.assertEqual(f(2, 5, self.policy(annual_deduct_sick=False)), 2)
        self.assertEqual(f(2, 5, self.policy(annual_deduct_sick=True)), 7)
        self.assertEqual(f(2, 5, self.policy(annual_deduct_unpaid_absence=False, annual_deduct_sick=True)), 5)

    def test_counts_validated(self):
        from app.leave_policy_preview import annual_deductible_count as f
        for bad in ((-1, 0), (0, -1), (True, 0), (0, 1.5)):
            with self.assertRaises(ValueError):
                f(*bad, self.policy())

    def test_year_basis_allowed_values_only(self):
        self.policy(annual_year_basis='hire_anniversary')
        with self.assertRaises(ValueError):
            self.policy(annual_year_basis='rolling')

    def test_sick_thresholds_must_be_unique_positive_tuple(self):
        for bad in ((), (90, 90), (0, 5), [90], (True,)):
            with self.assertRaises(ValueError):
                self.policy(sick_alert_thresholds=bad)

    def test_static_sick_month_source_and_no_threshold_consumer(self):
        """STATIC: monthly sick = code 'S' entries; holiday credit skipped for S days unless policy flag;
        sick_alert_thresholds is not read by any other app module outside policy edit/store/routes/preview."""
        m = _read('app/leave_month_earning.py')
        self.assertIn("entries[d]['code']!='S'", m)
        self.assertIn("sick_days=counts['S']", m)
        consumers = []
        for fn in sorted(os.listdir(os.path.join(ROOT, 'app'))):
            if fn.endswith('.py') and 'sick_alert_thresholds' in _read('app/' + fn):
                consumers.append(fn)
        allowed = {'leave_policy_preview.py', 'leave_policy_store.py', 'leave_policy_routes.py', 'leave_policy_edit.py'}
        self.assertTrue(set(consumers) <= allowed | {'i18n.py'}, consumers)


def _load_recompute():
    """STATIC-LOAD: compile the real recompute_payroll_item text from payroll_workflow.py
    (module itself needs pydantic), with the real payroll_math helpers."""
    from app.payroll_math import absence_deduction, money, overtime_hourly_rate
    tree = ast.parse(_read('app/payroll_workflow.py'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'recompute_payroll_item')
    ns = dict(PayrollItem=object, PayrollRun=object, Decimal=Decimal, absence_deduction=absence_deduction, money=money, overtime_hourly_rate=overtime_hourly_rate)
    exec(compile(ast.Module([node], []), 'payroll_workflow.recompute_payroll_item', 'exec'), ns)
    return ns['recompute_payroll_item']


def _item(**kw):
    base = dict(salary_basis=Decimal('6000'), performance_bonus=Decimal('0'), daily_hours=Decimal('8'),
                overtime_hours=Decimal('10'), overtime_approved=True, overtime_amount=Decimal('0'),
                overtime_daily_digest=None, base_salary=Decimal('6000'), attendance_bonus=Decimal('0'),
                occupancy_bonus=Decimal('0'), other_bonus=Decimal('0'), staff_reward_amount=Decimal('0'),
                absence_days=Decimal('0'), absence_deduction=Decimal('0'), lateness_deduction=Decimal('0'),
                insurance_share=Decimal('0'), taxes=Decimal('0'), other_deductions=Decimal('0'),
                advance_deduction=Decimal('0'), staff_sanction_amount=Decimal('0'), net_pay=None, employee_id=1)
    base.update(kw)
    return SimpleNamespace(**base)


RUN = SimpleNamespace(period_start=date(2026, 10, 1))


class OldMonthlyOvertimeSource(unittest.TestCase):
    """SOURCE 3: old monthly overtime = PayrollItem.overtime_hours (stored, manual/approved)
    x overtime_hourly_rate, in app/payroll_workflow.py::recompute_payroll_item, used only
    while overtime_daily_digest is None. Rate: app/payroll_math.py::overtime_hourly_rate."""

    def test_rate_is_basic_plus_performance_over_30_times_divisor(self):
        from app.payroll_math import overtime_hourly_rate as r
        self.assertEqual(r(Decimal('6000'), Decimal('0'), Decimal('8')), Decimal('25'))
        self.assertEqual(r(Decimal('6000'), Decimal('1200'), Decimal('8')), Decimal('30'))

    def test_rate_refuses_non_decimal_negative_or_nonpositive_divisor(self):
        from app.payroll_math import overtime_hourly_rate as r
        for args in ((6000, Decimal(0), Decimal(8)), (Decimal(-1), Decimal(0), Decimal(8)),
                     (Decimal(1), Decimal(-1), Decimal(8)), (Decimal(1), Decimal(0), Decimal(0)),
                     (Decimal('NaN'), Decimal(0), Decimal(8))):
            with self.assertRaises(ValueError):
                r(*args)

    def test_monthly_amount_only_when_approved(self):
        f = _load_recompute()
        it = _item(); f(it, RUN)
        self.assertEqual(it.overtime_amount, Decimal('250.00'))
        it = _item(overtime_approved=False, overtime_amount=Decimal('99')); f(it, RUN)
        self.assertEqual(it.overtime_amount, Decimal('0.00'))

    def test_daily_digest_amount_is_not_overwritten_by_monthly_formula(self):
        f = _load_recompute()
        it = _item(overtime_daily_digest='a' * 64, overtime_amount=Decimal('77.77')); f(it, RUN)
        self.assertEqual(it.overtime_amount, Decimal('77.77'))
        self.assertEqual(it.net_pay, Decimal('6077.77'))

    def test_hours_rounding_half_up_to_cents(self):
        f = _load_recompute()
        it = _item(overtime_hours=Decimal('0.02'), salary_basis=Decimal('6000'), daily_hours=Decimal('8')); f(it, RUN)
        self.assertEqual(it.overtime_amount, Decimal('0.50'))
        it = _item(overtime_hours=Decimal('1'), salary_basis=Decimal('100'), daily_hours=Decimal('8')); f(it, RUN)
        self.assertEqual(it.overtime_amount, Decimal('0.42'))  # 100/240 = 0.41666..

    def test_static_approval_reset_when_hours_or_performance_change(self):
        """STATIC: update path clears approval when overtime_hours or performance_bonus is in the changed keys;
        approval refused when daily overtime is required."""
        s = _read('app/payroll_workflow.py')
        self.assertIn('for key in ("overtime_hours", "performance_bonus")', s)
        self.assertIn('item.overtime_approved = False', s)
        self.assertIn("Daily overtime requires daily source review, not monthly approval.", s)
        self.assertIn("Positive overtime hours are required before approval.", s)


class PerformanceBonusSource(unittest.TestCase):
    """SOURCE 4: performance bonus = saved PayrollItem.performance_bonus (selected month,
    models.py). No maximum or manager score exists. Consumers: overtime rate
    (payroll_math), leave cash (leave_cash_math.calculate_leave_cash), gross pay
    (recompute_payroll_item), preview adapter (pay_basis_source)."""

    def test_leave_cash_is_basic_plus_performance_over_30(self):
        from app.leave_cash_math import calculate_leave_cash as c
        r = c(days=Decimal('2'), basic_salary=Decimal('6000'), performance_bonus=Decimal('1200'))
        self.assertEqual(r.cash_egp, Decimal('480.00'))
        self.assertEqual(r.daily_rate_exact, Fraction(240))

    def test_leave_cash_rounds_once_half_up_and_keeps_exact_fraction_days(self):
        from app.leave_cash_math import calculate_leave_cash as c
        r = c(days=Fraction(1, 3), basic_salary=Decimal('100'), performance_bonus=Decimal('0'))
        self.assertEqual(r.cash_egp, Decimal('1.11'))
        r = c(days=Decimal('1'), basic_salary=Decimal('0.15'), performance_bonus=Decimal('0'))
        self.assertEqual(r.cash_egp, Decimal('0.01'))  # 0.005 rounds up

    def test_leave_cash_refuses_bad_inputs(self):
        from app.leave_cash_math import calculate_leave_cash as c
        for kw in (dict(days=0, basic_salary=Decimal(1), performance_bonus=Decimal(0)),
                   dict(days=1, basic_salary=1, performance_bonus=Decimal(0)),
                   dict(days=1, basic_salary=Decimal(1), performance_bonus=Decimal(-1))):
            with self.assertRaises(ValueError):
                c(**kw)

    def test_performance_bonus_is_in_gross_and_other_bonuses_not_in_ot_rate(self):
        f = _load_recompute()
        it = _item(performance_bonus=Decimal('1200'), overtime_hours=Decimal('0'), other_bonus=Decimal('500'),
                   attendance_bonus=Decimal('100')); f(it, RUN)
        self.assertEqual(it.net_pay, Decimal('7800.00'))  # 6000+1200+500+100
        it = _item(performance_bonus=Decimal('1200'), other_bonus=Decimal('500')); f(it, RUN)
        self.assertEqual(it.overtime_amount, Decimal('300.00'))  # 10h * 30, other_bonus excluded

    def test_static_saved_values_and_no_cap(self):
        m = _read('app/models.py')
        self.assertIn('performance_bonus: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)', m)
        pb = _read('app/pay_basis_source.py')
        self.assertIn('performance_bonus=item.performance_bonus', pb)
        self.assertIn('No maximum award', pb)


if __name__ == '__main__':
    unittest.main()
