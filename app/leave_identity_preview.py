"""Pure identity-evidence preview for a later reviewed leave workbook import.

No workbook access, persistence, automatic linking or fuzzy/name-transliteration
matching. Caller supplies already verified host identities. Exact source strings
are retained. Ambiguous/conflicting evidence never picks the first employee.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EmployeeIdentity:
    employee_id: int
    code: str
    name: str


@dataclass(frozen=True)
class IdentityMatch:
    employee_id: int | None
    issue: str | None


def preview_identity_match(*, source_code: str | None, source_name: str | None,
                           identities: tuple[EmployeeIdentity, ...]) -> IdentityMatch:
    """Only surrounding whitespace is ignored, not case/script/spelling.

    A supplied but unmatched ID is not silently replaced with a name match.
    If both ID and name are supplied, they must agree exactly. A missing ID
    can use a unique exact name, with duplicate names explicitly unresolved.
    """
    for value in (source_code, source_name):
        if value is not None and not isinstance(value, str):
            raise TypeError('Source identity must be explicit text or unknown.')
    for row in identities:
        if (isinstance(row.employee_id, bool) or not isinstance(row.employee_id, int)
                or row.employee_id < 1 or not isinstance(row.code, str)
                or not row.code.strip() or not isinstance(row.name, str) or not row.name.strip()):
            raise ValueError('Resolved host identity evidence required.')
    if len({row.employee_id for row in identities}) != len(identities):
        raise ValueError('Host identity coverage contains duplicate employee IDs.')
    code, name = (source_code or '').strip(), (source_name or '').strip()
    if code:
        matches = [row for row in identities if row.code.strip() == code]
        if not matches:
            return IdentityMatch(None, 'employee_code_not_found')
        if len(matches) != 1:
            return IdentityMatch(None, 'duplicate_employee_code')
        if name and matches[0].name.strip() != name:
            return IdentityMatch(None, 'employee_code_name_conflict')
        return IdentityMatch(matches[0].employee_id, None)
    if not name:
        return IdentityMatch(None, 'missing_employee_identity')
    matches = [row for row in identities if row.name.strip() == name]
    if not matches:
        return IdentityMatch(None, 'employee_name_not_found')
    if len(matches) != 1:
        return IdentityMatch(None, 'duplicate_employee_name')
    return IdentityMatch(matches[0].employee_id, None)
