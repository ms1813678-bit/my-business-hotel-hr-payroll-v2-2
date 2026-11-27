"""Role-filtered export of change-log rows for one device. Pure: no database or network of its own.

The caller passes the UNFILTERED rows from sync_change_log (export_changes) and the device role taken
from the credential bound at pairing. Anything a role may not read is dropped before it leaves the
main device. Fail-closed: unknown entity, role or field yields nothing.

Row shape (dict or attribute object): entity, uid, field, value, main_seq, origin_device_id,
origin_counter, restore_epoch.
"""
from __future__ import annotations

from app.sync_visibility import ROLES, derive_branch_name, visible_columns

_OUT_KEYS = ("entity", "uid", "field", "value", "main_seq", "origin_device_id", "origin_counter", "restore_epoch")
BRANCH_REF = "branch_id"
BRANCH_NAME = "branch_name"


def _get(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _column_for(field):
    """Visibility column a logged field maps to. A parent reference may be logged as <x>_uid."""
    if isinstance(field, str) and field.endswith("_uid"):
        return field[:-4] + "_id"
    return field


def can_write(role, entity, field):
    """True when a device of this role may send an edit to this field. Derived columns are never writable."""
    if role not in ROLES or not isinstance(entity, str) or not isinstance(field, str):
        return False
    if field == BRANCH_NAME or field in ("id", "sync_uid"):
        return False
    return _column_for(field) in visible_columns(entity, role)


def validate(role, entity, field):
    """READ visibility only: True = this role may see this field, False = a hidden field.

    Do NOT use this to authorize a write. Edit rights per role are a separate rule nobody has decided;
    app/sync_apply.py takes an explicit may_edit policy for that and refuses to plan without one.
    """
    return can_write(role, entity, field)


def can_read(role, entity, field):
    return can_write(role, entity, field)


def filter_rows(rows, role, branch_name_for=None):
    """Return plain dicts of the rows this role may receive, in input order.

    branch_name_for(uid) -> Branch row/name or None. When given, a role that may read branch_name gets a
    derived employees/branch_name row wherever the log shows a branch_id change; the id never leaves.
    """
    if role not in ROLES:
        return []
    out = []
    for row in rows:
        entity, field = _get(row, "entity"), _get(row, "field")
        if entity == "employees" and field == BRANCH_REF:
            if branch_name_for is not None and BRANCH_NAME in visible_columns(entity, role):
                branch = branch_name_for(_get(row, "uid"))
                name = branch if isinstance(branch, str) else derive_branch_name(branch)
                item = {k: _get(row, k) for k in _OUT_KEYS}
                item["field"], item["value"] = BRANCH_NAME, name
                out.append(item)
            continue
        if not can_read(role, entity, field):
            continue
        out.append({k: _get(row, k) for k in _OUT_KEYS})
    return out


def export_for_device(db, role, since_main_seq, fetch, branch_name_for=None):
    """fetch is sync_change_log's export_changes(db, since_main_seq); kept injectable so this stays pure."""
    return filter_rows(fetch(db, since_main_seq), role, branch_name_for)
