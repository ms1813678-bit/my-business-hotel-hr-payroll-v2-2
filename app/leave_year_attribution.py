"""Explicit exact attribution of an existing ledger amount, never a new credit.

A caller must verify the source ledger identity and current HR authority in its
transaction. This pure plan cannot confer either or complete a year close.
"""
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
import re

from app.leave_exact_amount import as_fraction, canonical_amount, exact_sum


@dataclass(frozen=True)
class YearAttribution:
    ledger_id: int
    employee_id: int
    leave_type_id: int
    source_digest: str
    source_amount: Fraction
    allocations: tuple
    reviewed_on: date
    reason: str
    attribution_digest: str
    ledger_delta: Fraction = Fraction(0)


def plan_year_attribution(*, ledger_id, employee_id, leave_type_id, source_digest,
                          source_amount, allocations, reviewed_on, reason):
    """A complete explicit signed split must conserve the existing amount.

    Positive credit is never mixed with debt to inflate both. Year labels are
    supplied by the reviewer, never inferred from a row's creation timestamp.
    Every active tracked type is representable by stable type ID.
    """
    if any(type(v) is not int or not 1 <= v <= 2147483647
           for v in (ledger_id, employee_id, leave_type_id)):
        raise ValueError('Resolved ledger, employee and leave type identities required.')
    if not isinstance(source_digest, str) or re.fullmatch('[0-9a-f]{64}', source_digest) is None:
        raise ValueError('Exact source digest required.')
    if type(reviewed_on) is not date or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
        raise ValueError('Explicit review date and bounded attribution reason required.')
    if type(allocations) is not tuple or not 1 <= len(allocations) <= reviewed_on.year:
        raise ValueError('Explicit bounded year allocations required.')
    amount = as_fraction(source_amount)
    values = []
    seen = set()
    for item in allocations:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError('Each allocation needs an explicit year and exact amount.')
        year, value = item
        if type(year) is not int or not 1 <= year <= reviewed_on.year or year in seen:
            raise ValueError('Unique nonfuture source years required.')
        value = as_fraction(value)
        if (amount > 0 and value < 0) or (amount < 0 and value > 0) or (amount == 0 and value != 0):
            raise ValueError('Attribution cannot manufacture offsetting credit and debt.')
        values.append((year, value)); seen.add(year)
    # exact_sum bounds every intermediate numerator/denominator; a bare sum() let 2000 large
    # coprime denominators grow quadratically (200 entries took ~8s) before conservation refused.
    if Fraction(exact_sum(v for _, v in values)) != amount:
        raise ValueError('Complete attribution must conserve the existing ledger amount exactly.')
    values = tuple(sorted(values))
    payload = dict(ledger_id=ledger_id, employee_id=employee_id, leave_type_id=leave_type_id,
        source_digest=source_digest, source_amount=canonical_amount(amount),
        allocations=[(y, canonical_amount(v)) for y, v in values],
        reviewed_on=reviewed_on.isoformat(), reason=reason.strip())
    digest = sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return YearAttribution(ledger_id, employee_id, leave_type_id, source_digest, amount,
                           values, reviewed_on, reason.strip(), digest)
