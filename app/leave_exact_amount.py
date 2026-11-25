"""Exact mixed finite-decimal/rational days, never a rounded ledger shadow.

Finite-only results keep Decimal scale. A nonterminating result is Fraction.
Conversion to two-place text is display-only and must never be persisted as days.
"""
from decimal import Decimal, localcontext
from fractions import Fraction

MAX_DIGITS = 4096


def as_fraction(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Ledger contains nonfinite day evidence.')
        parts = value.as_tuple()
        if len(parts.digits) > MAX_DIGITS or abs(parts.exponent) > MAX_DIGITS or abs(value.adjusted()) > MAX_DIGITS:
            raise ValueError('Exact arithmetic resource bound exceeded.')
        result = Fraction(value)
    elif type(value) is Fraction:
        result = value
    else:
        raise ValueError('Decimal or exact Fraction days required.')
    # Avoid decimal conversion of attacker-sized integers before the bound check.
    if max(abs(result.numerator).bit_length(), result.denominator.bit_length()) > 13610:
        raise ValueError('Exact arithmetic resource bound exceeded.')
    if max(len(str(abs(result.numerator))), len(str(result.denominator))) > MAX_DIGITS + 1:
        raise ValueError('Exact arithmetic resource bound exceeded.')
    return result


def terminating_decimal(value):
    """Return exact Decimal or None. No Decimal division/context rounding."""
    value = as_fraction(value)
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return None
    places = max(twos, fives)
    coefficient = abs(value.numerator) * (2 ** (places - twos)) * (5 ** (places - fives))
    if coefficient.bit_length() > 13610 or len(str(coefficient)) > MAX_DIGITS + 1:
        raise ValueError('Exact arithmetic resource bound exceeded.')
    digits = tuple(map(int, str(coefficient)))
    return Decimal((int(value < 0), digits, -places))


def exact_sum(values):
    values = tuple(values)
    result = Fraction()
    from math import gcd
    for value in values:
        increment = as_fraction(value)
        # Bound lcm and products before constructing a possibly huge fraction.
        common = gcd(result.denominator, increment.denominator)
        left = increment.denominator // common
        right = result.denominator // common
        if (result.denominator.bit_length() + left.bit_length() > 13610
                or max(abs(result.numerator).bit_length() + left.bit_length(),
                       abs(increment.numerator).bit_length() + right.bit_length()) > 13610):
            raise ValueError('Exact arithmetic resource bound exceeded.')
        result += increment
        as_fraction(result)
    if all(isinstance(value, Decimal) for value in values):
        # Preserve finite canonical scale for existing snapshots and audit text.
        precision = max(50, max((v.adjusted() for v in values), default=0)
                        - min((v.as_tuple().exponent for v in values), default=0)
                        + len(str(len(values) + 1)) + 2)
        if precision > MAX_DIGITS:
            raise ValueError('Exact arithmetic resource bound exceeded.')
        with localcontext() as context:
            context.prec = precision
            total = sum(values, Decimal(0))
        if Fraction(total) != result:
            raise ValueError('Finite exact day sum refused.')
        return total
    decimal = terminating_decimal(result)
    return decimal if decimal is not None else result


def exact_text(value):
    as_fraction(value)
    if isinstance(value, Decimal):
        return format(value, 'f')
    decimal = terminating_decimal(value)
    return format(decimal, 'f') if decimal is not None else f'{value.numerator}/{value.denominator}'


def display_days(value):
    """Two decimals, ties to even. Display boundary only, pure integer rounding."""
    fraction = as_fraction(value)
    scaled = abs(fraction.numerator) * 100
    whole, remainder = divmod(scaled, fraction.denominator)
    if remainder * 2 > fraction.denominator or (remainder * 2 == fraction.denominator and whole % 2):
        whole += 1
    sign = '-' if fraction < 0 and whole else ''
    return f'{sign}{whole // 100}.{whole % 100:02d}'


def ledger_amount(row):
    """Exclusive representation, never substitute zero for missing rational truth."""
    numerator = getattr(row, 'exact_numerator', None)
    denominator = getattr(row, 'exact_denominator', None)
    if numerator is None and denominator is None:
        as_fraction(row.days_delta)
        return row.days_delta
    if numerator is None or denominator is None or row.days_delta is not None:
        raise ValueError('Ledger amount representations conflict.')
    if (not isinstance(numerator, Decimal) or not isinstance(denominator, Decimal)
            or not numerator.is_finite() or not denominator.is_finite()
            or numerator != numerator.to_integral_value() or denominator != denominator.to_integral_value()
            or denominator <= 0 or numerator == 0):
        raise ValueError('Whole numerator and positive denominator required.')
    as_fraction(numerator)
    as_fraction(denominator)
    if max(len(format(numerator,'f')),len(format(denominator,'f'))) > 32:
        raise ValueError('Rational ledger source exceeds exact source storage envelope.')
    result = Fraction(int(numerator), int(denominator))
    as_fraction(result)
    if result.numerator != numerator or result.denominator != denominator:
        raise ValueError('Reduced exact ledger representation required.')
    return result


def exact_negate(value):
    as_fraction(value)
    return value.copy_negate() if isinstance(value, Decimal) else -value


def canonical_amount(value):
    """Numeric identity independent of finite scale or storage representation."""
    value = as_fraction(value)
    return f'{value.numerator}/{value.denominator}'


def storage_amount(value):
    """Canonical exclusive storage. Rational lane is bounded separately from manual decimals."""
    value = as_fraction(value)
    finite = terminating_decimal(value)
    if finite is not None:
        return {'days_delta': finite, 'exact_numerator': None, 'exact_denominator': None}
    if max(len(str(abs(value.numerator))),len(str(value.denominator))) > 32:
        raise ValueError('Rational ledger source exceeds exact source storage envelope.')
    return {'days_delta': None, 'exact_numerator': Decimal(value.numerator), 'exact_denominator': Decimal(value.denominator)}
