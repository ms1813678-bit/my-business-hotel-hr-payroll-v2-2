"""Per-role readable columns for the four sync_uid entities. Isolated: no route, device, network or write is wired.

Fail-closed: an unknown entity, role or column yields nothing. The numeric `id` is never listed
(devices use `sync_uid`). Every real column must be classified, readable by some role or listed in
DENIED_ALL with its reason, which tests/test_sync_visibility.py enforces against the real models.

Grounding (read from source at 778196c, route access, NOT a confirmed sync-disclosure policy):
- employees: app/dependencies.py require_employee_read = system_admin + finance. The assistant has no
  employee directory access; it only sees the attendance-safe report fields (app/main.py
  _ATTENDANCE_SAFE_FIELDS: name, code, department, nationality, hire date). National id, passport,
  salary, phone, address and birth date are not in that set.
- departments, leave_types, leave_records: require_leave_read / require_attendance_read = all three roles.
Model of project notebook clause C5 (characterization, not authority): data a role may not see never goes to its device. The finance and assistant answers are recorded below the column sets (source: Mo's voice note).
"""
from __future__ import annotations

ROLE_ADMIN = "system_admin"
ROLE_FINANCE = "finance"
ROLE_ASSISTANT = "assistant_manager"
ROLES = (ROLE_ADMIN, ROLE_FINANCE, ROLE_ASSISTANT)

_EMPLOYEE_ALL = frozenset({
    "code", "full_name", "national_id", "nationality", "passport_number", "phone", "address",
    "fingerprint_id", "department_id", "position", "hire_date", "birth_date", "prior_insurance_years",
    "probation_start_date", "probation_end_date", "status", "basic_salary", "daily_hours", "created_at",
})
_EMPLOYEE_ASSISTANT = frozenset({"code", "full_name", "department_id", "nationality", "hire_date", "status", "branch_name"})
_DEPARTMENT = frozenset({"name", "active"})
_LEAVE_TYPE = frozenset({"code", "name_ar", "name_en", "name_zh", "is_paid", "tracks_balance", "active"})
_LEAVE_RECORD = frozenset({
    "employee_id", "leave_type_id", "start_date", "end_date", "calendar_days", "charged_days", "reason",
    "certificate_attached", "status", "created_by", "created_at", "confirmed_by", "confirmed_at",
    "cancelled_by", "cancelled_at", "cancellation_reason",
})

READABLE = {
    "employees": {ROLE_ADMIN: _EMPLOYEE_ALL, ROLE_FINANCE: _EMPLOYEE_ALL, ROLE_ASSISTANT: _EMPLOYEE_ASSISTANT},
    "departments": {role: _DEPARTMENT for role in ROLES},
    "leave_types": {role: _LEAVE_TYPE for role in ROLES},
    "leave_records": {role: _LEAVE_RECORD for role in ROLES},
}

# Columns that never leave the master for any role, with the source reason.
DENIED_ALL = {
    ("employees", "branch_id"): "no stable UID for branches in the foundation (sync_uid.OUT_OF_SCOPE_REFS)",
    ("employees", "photo_path"): "local filesystem path; the photo file is not a synced object here",
}

# Source: Mo's voice note, 2026-11-26 19:12:40 EET (WhatsApp wamid ...QTVBNjVDRERBMTRGRUJEOERBMkI1NzBGNjVGOTk3MEUA).
# Mo said: the finance device may see national id, passport, phone, address and birth date (already in _EMPLOYEE_ALL);
# the assistant device may see employee status (added above, "status") and the branch; the assistant device may see
# leave reason, cancellation reason and the actor names (already in _LEAVE_RECORD).
# Branch: decided by the parent on 2026-11-26 (technical design under Mo's answer): the assistant device receives
# the branch NAME only, as the derived read-only column "branch_name" (branches.name via employees.branch_id).
# The numeric branch_id stays denied (no stable UID for branches) and no Branch sync entity is added.
DERIVED_COLUMNS = {
    ("employees", "branch_name"): "branches.name looked up through employees.branch_id; the id itself is never sent",
}


def derive_branch_name(branch):
    """Branch name for the derived column: the name of a Branch row, or None when the employee has no branch."""
    name = getattr(branch, "name", None)
    return name if isinstance(name, str) and name else None


def visible_columns(entity, role):
    """Columns a role's device may receive, empty for anything unknown."""
    if not isinstance(entity, str) or not isinstance(role, str):
        return frozenset()
    return READABLE.get(entity, {}).get(role, frozenset())


def project(entity, role, values):
    """Keep only readable keys. Unknown keys, `id` and anything unclassified are dropped."""
    allowed = visible_columns(entity, role)
    if not isinstance(values, dict):
        return {}
    return {key: value for key, value in values.items() if key in allowed}
