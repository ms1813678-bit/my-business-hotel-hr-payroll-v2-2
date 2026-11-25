import pytest
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

from app.leave_exact_amount import (
    as_fraction,
    ledger_amount,
    storage_amount,
    canonical_amount,
    terminating_decimal,
)


def test_as_fraction_type_guards():
    for val in (True, False, 5, 0.5):
        with pytest.raises(ValueError, match='Decimal or exact Fraction'):
            as_fraction(val)


def test_as_fraction_decimal_exponent_and_digit_bounds():
    # Exponent beyond MAX_DIGITS
    with pytest.raises(ValueError, match='resource bound'):
        as_fraction(Decimal('1e-5000'))
    # Too many digits
    big_digits = Decimal('9' * 4100)  # 4100 > MAX_DIGITS (4096)
    with pytest.raises(ValueError, match='resource bound'):
        as_fraction(big_digits)


def test_as_fraction_fraction_bit_length_bound():
    # Numerator bit length > 13610 triggers bound before string length check
    huge_num = 2 ** 13611  # bit_length = 13612
    with pytest.raises(ValueError, match='resource bound'):
        as_fraction(Fraction(huge_num, 1))


def test_ledger_amount_invalid_decimal_components():
    base = SimpleNamespace(days_delta=None, exact_numerator=Decimal(1), exact_denominator=Decimal(7))
    # numerator not integral
    bad_num = SimpleNamespace(**{**vars(base), 'exact_numerator': Decimal('1.5')})
    with pytest.raises(ValueError):
        ledger_amount(bad_num)
    # denominator zero or negative
    for den in (Decimal(0), Decimal(-3)):
        bad_den = SimpleNamespace(**{**vars(base), 'exact_denominator': den})
        with pytest.raises(ValueError):
            ledger_amount(bad_den)
    # numerator zero
    zero_num = SimpleNamespace(**{**vars(base), 'exact_numerator': Decimal(0)})
    with pytest.raises(ValueError):
        ledger_amount(zero_num)


def test_ledger_amount_storage_envelope_limits():
    # numerator length > 32 characters
    long_num = Decimal('1' + '0' * 32)  # 33 digits
    row = SimpleNamespace(days_delta=None, exact_numerator=long_num, exact_denominator=Decimal(1))
    with pytest.raises(ValueError, match='storage envelope'):
        ledger_amount(row)
    # denominator length > 32 characters
    long_den = Decimal('1' + '0' * 32)
    row = SimpleNamespace(days_delta=None, exact_numerator=Decimal(1), exact_denominator=long_den)
    with pytest.raises(ValueError, match='storage envelope'):
        ledger_amount(row)


def test_storage_amount_rational_envelope_and_canonicalization():
    # Rational with numerator length > 32 should be rejected
    huge_num = int('9' * 33)  # 33 digits > 32, coprime to 7 so it is a real rational (an integer is a finite decimal)
    with pytest.raises(ValueError, match='source exceeds'):
        storage_amount(Fraction(huge_num, 7))
    # a 33-digit integer is a finite decimal, not the rational lane, so it is stored as days_delta
    assert storage_amount(Fraction(huge_num, 1))['days_delta'] == Decimal(huge_num)
    # Valid terminating rational returns days_delta
    assert storage_amount(Fraction(3, 4)) == {
        'days_delta': Decimal('.75'), 'exact_numerator': None, 'exact_denominator': None
    }
    # Non‑terminating rational returns exact_numerator/denominator fields
    frac = Fraction(1, 7)
    result = storage_amount(frac)
    assert result == {
        'days_delta': None,
        'exact_numerator': Decimal(1),
        'exact_denominator': Decimal(7),
    }


def test_canonical_amount_reduces_and_ignores_scale():
    assert canonical_amount(Fraction(2, 4)) == '1/2'
    assert canonical_amount(Fraction(-6, 9)) == '-2/3'
    assert canonical_amount(Decimal('1.00')) == '1/1'
    assert canonical_amount(Decimal('123.4500')) == '2469/20'
    assert canonical_amount(Decimal('0.50')) == canonical_amount(Decimal('0.5')) == '1/2'
    assert canonical_amount(Decimal('-1.50')) == '-3/2'
    assert canonical_amount(Decimal('0')) == '0/1'


def test_canonical_amount_matches_as_fraction_and_rejects_bad_types():
    raw = Fraction(123456789, 987654321)
    reduced = as_fraction(raw)
    assert canonical_amount(raw) == f'{reduced.numerator}/{reduced.denominator}'
    for bad in ('not a number', True, 1, 0.5, None):
        with pytest.raises(ValueError):
            canonical_amount(bad)
