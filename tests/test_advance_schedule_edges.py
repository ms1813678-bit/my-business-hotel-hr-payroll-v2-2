from datetime import date
from decimal import Decimal

import pytest

from app.advance_repayment_schedule import build_repayment_schedule


def _add_months(d: date, months: int) -> date:
    # simple month addition for tests (preserves day if possible)
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, 28)  # we only use day=1 in these tests, so this is safe
    return date(year, month, day)


def test_request_on_31st_and_december_rollover():
    # request on Jan 31 -> payroll_month = Jan 1, pay_date = Feb 5
    s = build_repayment_schedule(Decimal("100"), date(2026, 1, 31), 1)
    assert s[0]["payroll_month"] == date(2026, 1, 1)
    assert s[0]["pay_date"] == date(2026, 2, 5)

    # request on Dec 31 -> payroll_month = Dec 1, pay_date = Jan 5 (next year)
    s = build_repayment_schedule(Decimal("50"), date(2026, 12, 31), 1)
    assert s[0]["payroll_month"] == date(2026, 12, 1)
    assert s[0]["pay_date"] == date(2027, 1, 5)


def test_installment_count_type_rejection():
    # bool, float, and str must be rejected
    for bad in (True, 3.0, "3"):
        with pytest.raises(ValueError):
            build_repayment_schedule(Decimal("100"), date(2026, 10, 1), bad)


def test_count_36_accepted_37_rejected_and_count_equal_total_cents():
    # amount 0.36 has 36 cents; count == total_cents (36) is accepted
    s = build_repayment_schedule(Decimal("0.36"), date(2026, 6, 15), 36)
    assert len(s) == 36
    assert all(row["amount"] == Decimal("0.01") for row in s)
    assert sum((r["amount"] for r in s), Decimal("0.00")) == Decimal("0.36")

    # count 37 must be rejected (also triggers >36 rejection for larger amounts)
    with pytest.raises(ValueError):
        build_repayment_schedule(Decimal("100"), date(2026, 6, 15), 37)


def test_amount_coercion_from_int_str_float_and_quantize_behavior():
    # int coerced
    s_int = build_repayment_schedule(100, date(2026, 7, 1), 1)
    # str coerced
    s_str = build_repayment_schedule("100", date(2026, 7, 1), 1)
    # float coerced via str(float) then quantized to cents
    s_float = build_repayment_schedule(100.0, date(2026, 7, 1), 1)

    assert s_int[0]["amount"] == Decimal("100.00")
    assert s_str[0]["amount"] == Decimal("100.00")
    assert s_float[0]["amount"] == Decimal("100.00")


@pytest.mark.parametrize(
    "amount,count",
    [
        (Decimal("10.00"), 3),
        (Decimal("1234.56"), 7),
        (Decimal("0.99"), 9),
        (Decimal("1000.00"), 36),
        (Decimal("0.10"), 1),
    ],
)
def test_metamorphic_properties_for_various_amounts_and_counts(amount, count):
    schedule = build_repayment_schedule(amount, date(2026, 11, 19), count)

    # installment_number sequence 1..n
    assert [row["installment_number"] for row in schedule] == list(range(1, count + 1))

    # payroll_months are first-of-month and strictly consecutive
    payrolls = [row["payroll_month"] for row in schedule]
    assert all(pm.day == 1 for pm in payrolls)
    for i in range(1, len(payrolls)):
        prev = payrolls[i - 1]
        expected = _add_months(prev, 1)
        # expected day preserved as 1
        assert payrolls[i].year == expected.year and payrolls[i].month == expected.month

    # pay_date is the 5th of the month after payroll_month
    for row in schedule:
        pm = row["payroll_month"]
        expected_pay_month = _add_months(pm, 1)
        assert row["pay_date"] == date(expected_pay_month.year, expected_pay_month.month, 5)

    # amounts sum exactly to the quantized amount and each is quantized to cents
    total = sum((r["amount"] for r in schedule), Decimal("0.00"))
    assert total == amount.quantize(Decimal("0.01"))

    # amounts are non-increasing and differ by at most $0.01
    amounts = [r["amount"] for r in schedule]
    for a, b in zip(amounts, amounts[1:]):
        assert a >= b
        assert (a - b) <= Decimal("0.01")
