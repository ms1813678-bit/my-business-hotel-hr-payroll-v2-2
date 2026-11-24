"""Pure day-only before/after preview for later authorized recalculation.

No persistence, role decisions, month reopening or ledger mutation. Preview
acceptance does not authorize applying it. A write service must re-check current
revision/policy/source digest and owner-confirmed operator scope atomically.
"""
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from app.leave_balance_rules import exact_day_difference, _bounded_amount


@dataclass(frozen=True)
class BalanceChange:
    employee_id: int
    code: str
    before: Decimal
    after: Decimal
    delta: Decimal


@dataclass(frozen=True)
class RecalculationPreview:
    changes: tuple[BalanceChange, ...]
    source_digest: str
    preview_digest: str


def _encode_amount(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError('Balances must be finite Decimal days.')
    _bounded_amount(value)
    return format(value, 'f')


def preview_recalculation(before: dict[tuple[int, str], Decimal],
                          after: dict[tuple[int, str], Decimal], *,
                          source_digest: str) -> RecalculationPreview:
    if (not isinstance(source_digest, str) or len(source_digest) != 64 or any(c not in '0123456789abcdef' for c in source_digest)):
        raise ValueError('Explicit current source digest is required.')
    # A missing balance is unknown, never an inferred zero or deleted employee.
    if before.keys() != after.keys():
        raise ValueError('Employee/type coverage changed; resolve missing evidence before preview.')
    changes = []
    payload = []
    for employee_id, code in sorted(before):
        if isinstance(employee_id, bool) or not isinstance(employee_id, int) or employee_id < 1:
            raise ValueError('A resolved employee ID is required.')
        if code not in {'W', 'H', 'A'}:
            raise ValueError('Only weekly, holiday and annual day balances can be previewed.')
        old, new = before[employee_id, code], after[employee_id, code]
        old_text, new_text = _encode_amount(old), _encode_amount(new)
        delta = exact_day_difference(new, old)
        if old != new:
            changes.append(BalanceChange(employee_id, code, old, new, delta))
        # Include unchanged rows too so scope cannot silently change after review.
        payload.append([employee_id, code, old_text, new_text])
    encoded = json.dumps({'source': source_digest, 'balances': payload},
                         separators=(',', ':'), ensure_ascii=True).encode('ascii')
    return RecalculationPreview(tuple(changes), source_digest, hashlib.sha256(encoded).hexdigest())
