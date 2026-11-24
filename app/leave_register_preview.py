"""Pure evidence normalization for later preview/import/register workflows.

Does not persist, authorize, match employees, infer attendance, apply a policy,
execute workbook formulas or change existing attendance/leave records.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedRegisterCode:
    code: str | None
    issue: str | None


def normalize_register_code(value: object, *, explicit_present=False) -> NormalizedRegisterCode:
    """An imported blank stays unknown; explicit present is a separate decision.

    Formula/count cells are never interpreted as daily status. Unknown values
    are reported rather than coerced into present, U, or another leave code.
    """
    if not isinstance(explicit_present, bool):
        raise TypeError("Explicit-present evidence flag must be boolean.")
    if value is None or (isinstance(value, str) and not value.strip()):
        return NormalizedRegisterCode('' if explicit_present else None, None)
    if explicit_present:
        return NormalizedRegisterCode(None, 'present_evidence_conflicts_with_code')
    if not isinstance(value, str):
        return NormalizedRegisterCode(None, 'unknown_day_code')
    normalized = value.strip().upper()
    if normalized.startswith('='):
        return NormalizedRegisterCode(None, 'formula_is_not_day_evidence')
    if normalized in {'U', 'A', 'S', 'H', 'W', '—'}:
        return NormalizedRegisterCode(normalized, None)
    return NormalizedRegisterCode(None, 'unknown_day_code')
