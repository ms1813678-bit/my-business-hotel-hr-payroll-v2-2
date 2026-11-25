"""Read-only internal UID snapshot primitive, not a device export or role policy.

Only explicit columns are selected. No version/history, planner, write, route,
serialization, cache or network contract is established by this primitive.
The authenticated administrator-local caller must separately check authority.
"""
from dataclasses import dataclass
from sqlalchemy import select
from app.sync_uid import ENTITIES, PARENT_REFS, OUT_OF_SCOPE_REFS, UnknownEntity, UnknownParent, OutOfScopeRef, MalformedShape
from app.sync_uid_mixin import canonical_uid


@dataclass(frozen=True)
class UidSnapshot:
    entity: str
    uid: str
    values: dict


def read_uid_snapshot(db, entity, uid, fields):
    """Read exact selected columns and parent UIDs in one SELECT.

    The caller owns its transaction/snapshot isolation and disclosure authority.
    Pending ORM writes refuse without flush. Previously executed Core/raw writes
    are not detectable by this check; use a database-enforced read-only transaction
    when that guarantee is required. No ORM entities or lazy relationships
    are loaded. No full-row default and no authoritative revision is returned.
    """
    if db.new or db.dirty or db.deleted:
        raise MalformedShape('snapshot requires a session without pending writes')
    if not isinstance(entity, str) or entity not in ENTITIES:
        raise UnknownEntity('entity is outside UID snapshot scope')
    canonical_uid(uid)
    if not isinstance(fields, (tuple, list)) or not fields or any(type(f) is not str for f in fields) or len(set(fields)) != len(fields):
        raise MalformedShape('fields must be an explicit nonempty distinct column list')
    model = ENTITIES[entity]
    table = model.__table__
    if any(f not in table.c or f in ('id', 'sync_uid') for f in fields):
        raise MalformedShape('only explicit nonidentity scalar columns are supported')
    # Branch has no stable UID in this foundation, even if its value is NULL.
    if set(fields) & set(OUT_OF_SCOPE_REFS.get(entity, {})):
        raise OutOfScopeRef('selected reference has no UID foundation')
    source = table
    columns = []
    parents = []
    for f in fields:
        parent_entity = PARENT_REFS.get(entity, {}).get(f)
        if parent_entity:
            parent = ENTITIES[parent_entity].__table__.alias('parent_' + f)
            source = source.outerjoin(parent, table.c[f] == parent.c.id)
            label = f[:-3] + '_uid'
            columns.append(parent.c.sync_uid.label(label))
            parents.append(label)
        else:
            columns.append(table.c[f])
    with db.no_autoflush:
        row = db.execute(select(*columns).select_from(source).where(table.c.sync_uid == uid)).mappings().one_or_none()
    if row is None:
        raise UnknownParent('UID does not resolve in the requested entity')
    values = dict(row)
    for label in parents:
        if values[label] is None:
            raise UnknownParent('parent does not resolve to a UID')
        canonical_uid(values[label])
    return UidSnapshot(entity, uid, values)
