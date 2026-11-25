"""Additive, immutable, unique replication identity (sync_uid) for synced base entities.

Integer primary keys and foreign keys are untouched. After migration 0067 the column is NOT NULL,
unique (named uq_<table>_sync_uid), CHECKed to canonical lowercase UUID4, and immutable at the DB
level (trigger). New rows: the ORM assigns uuid4 (or keeps a supplied canonical device uid); Core/raw
SQL inserts that omit the column get a DB default (sync_uid_new(), pure SQL, no extension).
The model deliberately declares no server_default (it would reference a migration-owned function).
"""
import uuid

from sqlalchemy import CheckConstraint, String, UniqueConstraint, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

UID_REGEX = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class SyncUidError(ValueError):
    pass


class SyncUidImmutable(SyncUidError):
    pass


def canonical_uid(value) -> str:
    """Return value if it is a canonical lowercase UUID4 string, else raise."""
    if isinstance(value, str):
        try:
            u = uuid.UUID(value)
        except ValueError:
            u = None
        if u is not None and u.version == 4 and str(u) == value:
            return value
    raise SyncUidError("sync_uid must be a canonical lowercase UUID4 string")


def sync_uid_constraints(table: str):
    return (
        UniqueConstraint("sync_uid", name=f"uq_{table}_sync_uid"),
        CheckConstraint(
            f"sync_uid ~ '{UID_REGEX}'", name=f"ck_{table}_sync_uid_v4"
        ).ddl_if(dialect="postgresql"),
    )


class SyncUidMixin:
    sync_uid: Mapped[str] = mapped_column(String(36), nullable=False, active_history=True)


@event.listens_for(SyncUidMixin, "before_insert", propagate=True)
def _uid_before_insert(mapper, connection, target):
    if target.sync_uid is None:
        target.sync_uid = str(uuid.uuid4())
    else:
        canonical_uid(target.sync_uid)


@event.listens_for(SyncUidMixin, "before_update", propagate=True)
def _uid_before_update(mapper, connection, target):
    hist = inspect(target).attrs.sync_uid.history
    if not hist.has_changes():
        return
    old = hist.deleted[0] if hist.deleted else None
    new = hist.added[0] if hist.added else None
    if old is not None and new != old:
        raise SyncUidImmutable("sync_uid is write-once and cannot be changed or cleared")
    if new is not None:
        canonical_uid(new)
