"""Shared manual ledger rules, not carry/grant/settlement authorization."""
from decimal import Context, Decimal, DecimalException, Inexact, Rounded, localcontext

# Resource envelope only, not an entitlement cap or financial rounding rule.
# Unsupported inputs refuse without changing an amount. Ordinary source precision
# is far below this bound; caller must never truncate to enter this envelope.
MAX_ARITHMETIC_DIGITS = 4096


def _bounded_amount(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError('Finite Decimal days required.')
    parts = value.as_tuple()
    if (len(parts.digits) > MAX_ARITHMETIC_DIGITS
            or abs(parts.exponent) > MAX_ARITHMETIC_DIGITS
            or abs(value.adjusted()) > MAX_ARITHMETIC_DIGITS):
        raise ValueError('Exact arithmetic resource bound exceeded; no amount rounded or recorded.')


def validate_manual_balance_change(transaction_type, days_delta):
    if not isinstance(transaction_type, str) or transaction_type not in {'opening', 'adjustment'}:
        raise ValueError('Manual balance changes must be opening or adjustment; carry and settlement require their reviewed workflow.')
    _bounded_amount(days_delta)
    if days_delta == 0:
        raise ValueError('Finite nonzero Decimal days required.')
    if transaction_type == 'opening' and days_delta < 0:
        raise ValueError('Negative opening is not permitted. Review negative carry as an explicit adjustment, not a new opening or grant.')


def preview_manual_balance(current, transaction_type, days_delta):
    validate_manual_balance_change(transaction_type, days_delta)
    _bounded_amount(current)
    precision = max(50, max(current.adjusted(), days_delta.adjusted())
                    - min(current.as_tuple().exponent, days_delta.as_tuple().exponent) + 2)
    if precision > MAX_ARITHMETIC_DIGITS:
        raise ValueError('Exact arithmetic resource bound exceeded; no amount rounded or recorded.')
    context = Context(prec=precision, Emin=-MAX_ARITHMETIC_DIGITS * 2,
                      Emax=MAX_ARITHMETIC_DIGITS * 2)
    context.traps[Inexact] = True
    context.traps[Rounded] = True
    try:
        with localcontext(context):
            result = current + days_delta
    except DecimalException as exc:
        raise ValueError('Exact manual balance arithmetic refused; no amount recorded.') from exc
    if not result.is_finite():
        raise ValueError('Finite exact result required.')
    return result


def exact_day_difference(after, before):
    """Exact preview arithmetic, without authorizing any transaction kind.

    Shares the ledger resource envelope but permits zero differences. Context
    precision/rounding supplied by callers cannot truncate source evidence.
    """
    _bounded_amount(after)
    _bounded_amount(before)
    precision = max(50, max(after.adjusted(), before.adjusted())
                    - min(after.as_tuple().exponent, before.as_tuple().exponent) + 2)
    if precision > MAX_ARITHMETIC_DIGITS:
        raise ValueError('Exact day arithmetic resource bound exceeded; no amount rounded.')
    context = Context(prec=precision, Emin=-MAX_ARITHMETIC_DIGITS * 2,
                      Emax=MAX_ARITHMETIC_DIGITS * 2)
    context.traps[Inexact] = True
    context.traps[Rounded] = True
    try:
        with localcontext(context):
            result = after - before
    except DecimalException as exc:
        raise ValueError('Exact day arithmetic refused; no amount rounded.') from exc
    _bounded_amount(result)
    return result
