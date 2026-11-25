# Isolated test candidate: no app change, no migration, no new policy.
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.leave_exact_amount import (
    as_fraction,
    canonical_amount,
    display_days,
    exact_negate,
    exact_sum,
    exact_text,
    ledger_amount,
    storage_amount,
    terminating_decimal,
)
from app.leave_late_year_preservation import describe_late_year_annual
from app.leave_release_preview import annual_release_preview


def preservation_args(hire=date(2026, 12, 15), **overrides):
    args = dict(
        employee_id=1,
        hire_date=hire,
        earning_year=2026,
        earned_annual=Decimal("0.68548387096774193548387096774193548387096774193548"),
        annual_used_from_that_earning_year=Decimal(0),
        release_evaluated_on=date(2026, 12, 31),
        full_calendar_month_worked=True,
        earning_year_attribution_confirmed=True,
    )
    args.update(overrides)
    return args


def test_decimal_fraction_mix_exact_amount_helpers():
    d = Decimal("0.125")
    f = Fraction(1, 3)

    assert as_fraction(d) == Fraction(1, 8)
    assert as_fraction(f) == f

    total = exact_sum((d, f))
    assert total == Fraction(11, 24)
    assert isinstance(total, Fraction)
    assert exact_text(total) == "11/24"

    assert exact_sum((Decimal("0.125"), Fraction(3, 8))) == Decimal("0.5")
    assert exact_text(Decimal("1.2300")) == "1.2300"
    assert exact_text(Fraction(1, 8)) == "0.125"
    assert display_days(Fraction(1, 3)) == "0.33"

    assert terminating_decimal(Fraction(1, 8)) == Decimal("0.125")
    assert terminating_decimal(Fraction(1, 3)) is None

    assert exact_negate(Decimal("0.125")) == Decimal("-0.125")
    assert exact_negate(Fraction(1, 3)) == Fraction(-1, 3)

    assert canonical_amount(Decimal("1.2500")) == "5/4"
    assert canonical_amount(Fraction(5, 4)) == "5/4"

    finite_storage = storage_amount(Fraction(5, 4))
    assert finite_storage == {
        "days_delta": Decimal("1.25"),
        "exact_numerator": None,
        "exact_denominator": None,
    }

    rational_storage = storage_amount(Fraction(1, 3))
    assert rational_storage == {
        "days_delta": None,
        "exact_numerator": Decimal(1),
        "exact_denominator": Decimal(3),
    }


def test_ledger_amount_decimal_and_rational_storage_lanes():
    class Row:
        days_delta = Fraction(1, 3)
        exact_numerator = None
        exact_denominator = None

    assert ledger_amount(Row()) == Fraction(1, 3)

    class RationalRow:
        days_delta = None
        exact_numerator = Decimal(1)
        exact_denominator = Decimal(3)

    assert ledger_amount(RationalRow()) == Fraction(1, 3)

    class ConflictRow:
        days_delta = Decimal("0.333")
        exact_numerator = Decimal(1)
        exact_denominator = Decimal(3)

    with pytest.raises(ValueError, match="representations conflict"):
        ledger_amount(ConflictRow())


def test_high_precision_decimal_remainders_and_sums_stay_exact():
    a = Decimal("0.123456789012345678901234567890123456789012345678901234567890")
    b = Decimal("0.000000000000000000000000000000000000000000000000000001")
    c = Decimal("0.876543210987654321098765432109876543210987654321098765432109")

    assert exact_sum((a, b)) == terminating_decimal(Fraction(a) + Fraction(b))
    assert exact_sum((a, b)) == Decimal(
        "0.123456789012345678901234567890123456789012345678901235567890"
    )
    assert exact_sum((a, c)) == terminating_decimal(Fraction(a) + Fraction(c))
    assert exact_sum((a, b, c)) == terminating_decimal(Fraction(a) + Fraction(b) + Fraction(c))

    nonterminating = exact_sum((a, Fraction(1, 7)))
    assert isinstance(nonterminating, Fraction)
    assert nonterminating == Fraction(a) + Fraction(1, 7)

    # Rounding occurs only at the display boundary; source evidence remains exact.
    source = Fraction(1, 3)
    assert display_days(source) == "0.33"
    assert exact_text(source) == "1/3"
    assert source == Fraction(1, 3)

    assert display_days(Fraction(1, 8)) == "0.12"  # ties to even
    assert display_days(Fraction(3, 8)) == "0.38"  # ties to even


def test_exact_sum_ignores_decimal_context_rounding():
    values = (
        Decimal("1234567890123456789012345678901234567890.123456789"),
        Decimal("0.0000000000000000000000000000000000000001"),
    )
    expected = Decimal(
        "1234567890123456789012345678901234567890.1234567890000000000000000000000000000001"
    )
    with localcontext() as context:
        context.prec = 4
        assert exact_sum(values) == expected


@pytest.mark.parametrize(
    "hire,first_release",
    [
        (date(2026, 11, 1), date(2026, 12, 1)),
        (date(2026, 11, 15), date(2027, 1, 1)),
        (date(2026, 11, 30), date(2027, 1, 1)),
        (date(2026, 12, 1), date(2027, 1, 1)),
        (date(2026, 12, 15), date(2027, 2, 1)),
        (date(2026, 12, 31), date(2027, 2, 1)),
    ],
)
def test_november_december_hires_preservation_and_release_boundary(hire, first_release):
    args = preservation_args(hire=hire)
    result = describe_late_year_annual(**args)

    assert result.exception_applies is True
    assert result.preserved_earned_remainder == args["earned_annual"]
    assert result.earliest_release_date == first_release

    # The descriptor refuses evaluation before Dec 31 of the earning year, so only
    # releases that fall after that date have a before/at boundary here.
    if first_release > date(2026, 12, 31):
        before = describe_late_year_annual(
            **{**args, "release_evaluated_on": first_release - date.resolution}
        )
        assert before.preserved_earned_remainder == args["earned_annual"]
        assert before.released_part == Decimal(0)
        assert before.held_part == args["earned_annual"]
        at = describe_late_year_annual(**{**args, "release_evaluated_on": first_release})
        assert at.preserved_earned_remainder == args["earned_annual"]
        assert at.released_part == args["earned_annual"]
        assert at.held_part == Decimal(0)
    else:
        assert result.released_part == args["earned_annual"] and result.held_part == Decimal(0)
        with pytest.raises(ValueError, match="not yet evaluated"):
            describe_late_year_annual(
                **{**args, "release_evaluated_on": date(2026, 12, 30)}
            )

    assert result.following_year_start == date(2027, 1, 1)
    assert result.following_year_end == date(2027, 12, 31)
    assert not result.posting_allowed
    assert not result.establishes_spendable_credit
    assert not result.cash_action_allowed


@pytest.mark.parametrize(
    "hire,as_of,full_month,expected_release,expected_reason",
    [
        (date(2026, 11, 1), date(2026, 11, 30), True, date(2026, 12, 1), "full_calendar_month_not_finished"),
        (date(2026, 11, 15), date(2026, 12, 31), True, date(2027, 1, 1), "full_calendar_month_not_finished"),
        (date(2026, 11, 30), date(2027, 1, 1), True, date(2027, 1, 1), "retroactive_earned_credit_eligible"),
        (date(2026, 12, 1), date(2026, 12, 31), True, date(2027, 1, 1), "full_calendar_month_not_finished"),
        (date(2026, 12, 15), date(2027, 1, 31), True, date(2027, 2, 1), "full_calendar_month_not_finished"),
        (date(2026, 12, 31), date(2027, 2, 1), True, date(2027, 2, 1), "retroactive_earned_credit_eligible"),
    ],
)
def test_release_preview_calendar_boundary_behavior(
    hire, as_of, full_month, expected_release, expected_reason
):
    result = annual_release_preview(
        hire_date=hire,
        as_of=as_of,
        full_calendar_month_worked=full_month,
    )
    assert result.earliest_release_date == expected_release
    assert result.reason == expected_reason
    assert result.released == (expected_reason == "retroactive_earned_credit_eligible")


@pytest.mark.parametrize(
    "earned,used,remainder",
    [
        (Decimal("1.2500"), Decimal("1.2500"), Decimal("0")),
        (Decimal("1.2500"), Decimal("0.2500"), Decimal("1.0000")),
        (Decimal("1.2500"), Decimal("1.5000"), None),
    ],
)
def test_reserved_vs_spent_attributed_to_earning_year(earned, used, remainder):
    result = describe_late_year_annual(
        **preservation_args(
            hire=date(2026, 11, 1),
            earned_annual=earned,
            annual_used_from_that_earning_year=used,
        )
    )
    if remainder is None:
        assert result.preserved_earned_remainder is None
        assert "negative_remainder_requires_separate_debt_carry" in result.issues
        assert result.released_part is None
        assert result.held_part is None
    else:
        assert result.preserved_earned_remainder == remainder
        assert result.released_part == remainder
        assert result.held_part == Decimal(0)


def test_unconfirmed_attribution_does_not_infer_opening_or_all_time_balance():
    result = describe_late_year_annual(
        **preservation_args(earning_year_attribution_confirmed=False)
    )
    assert result.preserved_earned_remainder is None
    assert result.released_part is None
    assert result.held_part is None
    assert "earned_year_attribution_unresolved_no_opening_inference" in result.issues


def test_negative_remainder_is_not_cash_or_spendable_credit():
    result = describe_late_year_annual(
        **preservation_args(
            earned_annual=Decimal("0.5"),
            annual_used_from_that_earning_year=Decimal("0.75"),
        )
    )
    assert result.preserved_earned_remainder is None
    assert result.released_part is None
    assert result.held_part is None
    assert "negative_remainder_requires_separate_debt_carry" in result.issues
    assert not result.posting_allowed
    assert not result.establishes_spendable_credit
    assert not result.cash_action_allowed


def test_exact_fraction_earned_amount_is_accepted_without_decimal_dust():
    # 85/124 is the exact Dec-15 earned value that the 50-digit Decimal above only approximates.
    earned = Fraction(85, 124)
    result = describe_late_year_annual(
        **preservation_args(earned_annual=earned, annual_used_from_that_earning_year=Decimal(0))
    )
    assert result.preserved_earned_remainder == earned
    assert type(result.preserved_earned_remainder) is Fraction
    assert result.released_part == 0 and result.held_part == earned
    assert exact_sum((result.released_part, result.held_part)) == earned


def test_exact_fraction_used_and_remainder_conserve_exactly():
    result = describe_late_year_annual(
        **preservation_args(hire=date(2026, 11, 1), earned_annual=Fraction(5, 28),
                            annual_used_from_that_earning_year=Fraction(1, 28))
    )
    assert result.preserved_earned_remainder == Fraction(1, 7)
    assert result.released_part == Fraction(1, 7) and result.held_part == 0
    with localcontext() as context:
        context.prec = 2
        again = describe_late_year_annual(
            **preservation_args(hire=date(2026, 11, 1), earned_annual=Fraction(5, 28),
                                annual_used_from_that_earning_year=Fraction(1, 28))
        )
    assert again == result


def test_exact_fraction_overspend_is_debt_not_credit_and_float_is_refused():
    result = describe_late_year_annual(
        **preservation_args(earned_annual=Fraction(1, 3),
                            annual_used_from_that_earning_year=Fraction(1, 2))
    )
    assert result.preserved_earned_remainder is None
    assert "negative_remainder_requires_separate_debt_carry" in result.issues
    for bad in (0.5, True, "1", None):
        with pytest.raises(ValueError):
            describe_late_year_annual(**preservation_args(earned_annual=bad))
