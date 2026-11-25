"""sync_uid <-> integer id mapping for the first replicated entities.

Scope (agreed increment): departments, employees, leave_types, leave_records. Pure mapping over the
real models: no applier, no disclosure policy, no network, no allocation of employee codes.
Every failure is closed: an unknown uid, a wrong-entity uid, a parent without a uid, or an FK to
an entity outside this scope raises instead of guessing.
"""
from sqlalchemy import select

from app.models import Department, Employee, LeaveRecord, LeaveType
from app.sync_uid_mixin import SyncUidError, canonical_uid

import uuid as _uuid

ENTITIES = {"departments": Department, "employees": Employee,
            "leave_types": LeaveType, "leave_records": LeaveRecord}
# integer FK column -> parent entity (payload key replaces "<x>_id" with "<x>_uid")
PARENT_REFS = {
    "employees": {"department_id": "departments"},
    "leave_records": {"employee_id": "employees", "leave_type_id": "leave_types"},
}
# FKs to entities not yet replicated: a non-null value cannot be mapped, so it fails closed.
OUT_OF_SCOPE_REFS = {"employees": {"branch_id": "branches"}}


class MalformedShape(SyncUidError):
    """Untrusted wire/value dict has the wrong shape (type, key form, ambiguity)."""


class UnknownEntity(SyncUidError):
    pass


class UnknownParent(SyncUidError):
    pass


class MissingUid(SyncUidError):
    pass


class OutOfScopeRef(SyncUidError):
    pass


def _model(entity):
    try:
        return ENTITIES[entity]
    except KeyError:
        raise UnknownEntity(f"entity not replicated by sync_uid: {entity!r}") from None


def ensure_uid(db, row) -> str:
    """Backfill-style one-time assignment (None -> uid). Never changes an existing uid."""
    if row.sync_uid is None:
        row.sync_uid = str(_uuid.uuid4())
        db.flush()
    return row.sync_uid


def resolve(db, entity, uid):
    """Row for (entity, uid) or None. A uid of another entity never resolves here."""
    model = _model(entity)
    canonical_uid(uid)
    return db.scalar(select(model).where(model.sync_uid == uid))


def require(db, entity, uid):
    row = resolve(db, entity, uid)
    if row is None:
        raise UnknownParent(f"no {entity} row has this sync_uid")
    return row


def _exact_id(v):
    if type(v) is not int or v < 1:           # not bool, not str, not float, positive only
        raise MalformedShape("integer id must be an exact positive int")
    return v


def _check_dict(values):
    if not isinstance(values, dict) or not all(isinstance(k, str) for k in values):
        raise MalformedShape("values must be a dict with string keys")


def uid_of(db, entity, int_id) -> str:
    _exact_id(int_id)
    row = db.get(_model(entity), int_id)
    if row is None:
        raise UnknownParent(f"no {entity} row with this id")
    if row.sync_uid is None:
        raise MissingUid(f"{entity} id has no sync_uid yet (backfill pending)")
    return row.sync_uid


def refs_to_uids(db, entity, values: dict) -> dict:
    """DB-form fields -> wire form: integer FK 'department_id' becomes 'department_uid'.

    Mapping only, NOT a field-policy validator: other keys pass through untouched. Rejected
    (MalformedShape): non-dict, raw 'id'/'sync_uid', any '<x>_uid' key already present (ambiguity),
    a null or non-exact-positive-int parent id. Non-null out-of-scope FK: OutOfScopeRef."""
    _model(entity)
    _check_dict(values)
    if "id" in values or "sync_uid" in values:
        raise MalformedShape("raw primary key / sync_uid is not accepted in field values")
    out = dict(values)
    for col in OUT_OF_SCOPE_REFS.get(entity, {}):
        if out.get(col) is not None:
            raise OutOfScopeRef(f"{entity}.{col} references an entity outside sync_uid scope")
    for col, parent in PARENT_REFS.get(entity, {}).items():
        key = col[:-3] + "_uid"
        if key in out:
            raise MalformedShape(f"{key} already present in DB-form values")
        if col in out:
            out[key] = uid_of(db, parent, out.pop(col))
    return out


def uids_to_refs(db, entity, values: dict) -> dict:
    """Wire form -> DB-form (inverse). Parents must already exist on this database.

    Wire input must carry parent uids only: raw 'id', raw FK columns (department_id, employee_id,
    leave_type_id, ...) and both forms together are rejected, never overwritten. Uids are
    per-table: a uid resolves only in its own parent entity. Mapping only, not a field-policy
    validator."""
    _model(entity)
    _check_dict(values)
    if "id" in values or "sync_uid" in values:
        raise MalformedShape("raw primary key / sync_uid is not accepted in field values")
    out = dict(values)
    for col in OUT_OF_SCOPE_REFS.get(entity, {}):
        if out.get(col) is not None or out.get(col[:-3] + "_uid") is not None:
            raise OutOfScopeRef(f"{entity}.{col} references an entity outside sync_uid scope")
    for col, parent in PARENT_REFS.get(entity, {}).items():
        key = col[:-3] + "_uid"
        if col in out:
            raise MalformedShape(f"raw FK column {col} is not accepted in wire values")
        if key in out:
            u = out.pop(key)
            if not isinstance(u, str):
                raise MalformedShape(f"{key} must be a canonical uid string")
            out[col] = require(db, parent, u).id
    return out
