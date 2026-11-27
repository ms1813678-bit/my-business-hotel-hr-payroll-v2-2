"""Append-only field change log for sync (part 1a of the sync core). No network, no apply, no screen.

What this module does:
- record_change(): write one field-level change inside the CALLER's transaction (never commits). Each
  device keeps its own counter and a hash chain over canonical rows, so a missing, reordered or edited
  row is detectable (verify_rows / verify_chain).
- stamp_main_seq(): on the main device only, number accepted rows once (NULL -> number, never changed).
- export_changes(): the UNFILTERED rows after a main_seq, in the shape app/sync_device_export.py expects.
  The export carries the NEW value only. old_value stays in the log for the audit trail and is never
  exported, so a value a role may not see cannot leak through the "old" side of a change.

Rules fixed here (SYNC_DESIGN section 3):
- local_time is a canonical UTC string, fixed width, microseconds: 2026-11-27T21:30:00.000000Z.
- Values are stored as snapshot_codec text (typed, canonical), never raw Python reprs.
- A parent reference is logged under the name <x>_uid with the parent's uid as the value, never an
  integer id. branch_id is refused until branches have a uid (open item in SYNC_DESIGN).
- Rows other than 'set' are markers with field '*': 'create' (a record was created, its fields follow as
  'set' rows) and 'delete'. export_changes returns them with op; app/sync_device_export.py drops
  field '*' today, so deletes do not reach devices until the apply part (1b) handles markers.
- A role's right to READ a field is not a right to EDIT it; this log checks only that the field exists
  for the entity (administrator column set). Edit rights are checked by the applier (part 1b).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models import SyncChangeLog
from app.snapshot_codec import SnapshotCodecError, decode_snapshot, encode_snapshot
from app.sync_uid import ENTITIES, OUT_OF_SCOPE_REFS
from app.sync_uid_mixin import SyncUidError, canonical_uid
from app.sync_visibility import ROLE_ADMIN, visible_columns

GENESIS_HASH = "0" * 64
MARKER_FIELD = "*"
OPS = ("set", "create", "delete")


class SyncLogError(ValueError):
    """Controlled rejection; messages never echo a supplied value."""


class ChainBroken(SyncLogError):
    pass


def canonical_time(value: datetime) -> str:
    """Fixed-width UTC string for an aware datetime. A naive datetime is refused, never guessed."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SyncLogError("local time must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def row_hash(prev_hash, device_id, counter, op, entity, uid, field, old_value, new_value, local_time, restore_epoch):
    payload = json.dumps([prev_hash, device_id, counter, op, entity, uid, field, old_value, new_value,
                          local_time, restore_epoch or ""], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _check_field(entity, field):
    if not isinstance(field, str) or not field:
        raise SyncLogError("field must be a non-empty string")
    column = field[:-4] + "_id" if field.endswith("_uid") else field
    if column in OUT_OF_SCOPE_REFS.get(entity, {}):
        raise SyncLogError("reference has no uid yet")
    if field in ("id", "sync_uid") or field.endswith("_id") or column not in visible_columns(entity, ROLE_ADMIN):
        raise SyncLogError("field is not a loggable column of this entity")


def _encode(entity, uid, field, value):
    if value is None:
        return None
    try:
        return encode_snapshot(entity, uid, {field: value}).decode("utf-8")
    except SnapshotCodecError:
        raise SyncLogError("value cannot be encoded") from None


def _lock_device(db, device_id):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "sync_change_log:" + device_id})


def record_change(db, *, device_id, entity, uid, field=MARKER_FIELD, old=None, new=None, op="set",
                  local_time=None, restore_epoch=None):
    """Append one change for this device inside the caller's transaction. Returns the new log row."""
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 64:
        raise SyncLogError("device id must be 1 to 64 characters")
    if op not in OPS:
        raise SyncLogError("unknown operation")
    if entity not in ENTITIES:
        raise SyncLogError("entity is not replicated")
    try:
        canonical_uid(uid)
    except SyncUidError:
        raise SyncLogError("uid must be a canonical uid") from None
    if op == "set":
        _check_field(entity, field)
        old_text, new_text = _encode(entity, uid, field, old), _encode(entity, uid, field, new)
    else:
        if field != MARKER_FIELD or old is not None or new is not None:
            raise SyncLogError("create and delete markers carry no field or value")
        old_text = new_text = None
    when = canonical_time(local_time if local_time is not None else datetime.now(timezone.utc))
    _lock_device(db, device_id)
    last = db.scalars(select(SyncChangeLog).where(SyncChangeLog.origin_device_id == device_id)
                      .order_by(SyncChangeLog.origin_counter.desc()).limit(1)).first()
    counter = 1 if last is None else last.origin_counter + 1
    prev = GENESIS_HASH if last is None else last.row_hash
    row = SyncChangeLog(origin_device_id=device_id, origin_counter=counter, restore_epoch=restore_epoch, op=op,
                        entity=entity, uid=uid, field=field, old_value=old_text, new_value=new_text,
                        local_time=when, prev_hash=prev,
                        row_hash=row_hash(prev, device_id, counter, op, entity, uid, field, old_text, new_text, when, restore_epoch))
    db.add(row)
    db.flush()
    return row


def verify_rows(rows):
    """rows: one device's rows ordered by counter. Raises ChainBroken on a gap, reorder or edited row."""
    prev, expected = GENESIS_HASH, 1
    for r in rows:
        if r.origin_counter != expected or r.prev_hash != prev:
            raise ChainBroken("counter gap or broken link")
        want = row_hash(prev, r.origin_device_id, r.origin_counter, r.op, r.entity, r.uid, r.field,
                        r.old_value, r.new_value, r.local_time, r.restore_epoch)
        if r.row_hash != want:
            raise ChainBroken("row content does not match its hash")
        prev, expected = r.row_hash, expected + 1
    return expected - 1


def verify_chain(db, device_id):
    rows = db.scalars(select(SyncChangeLog).where(SyncChangeLog.origin_device_id == device_id)
                      .order_by(SyncChangeLog.origin_counter)).all()
    return verify_rows(rows)


def stamp_main_seq(db, rows):
    """Main device only: give each unstamped row the next main_seq, in the order given. Idempotent."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('sync_change_log:main_seq'))"))
    top = db.execute(select(SyncChangeLog.main_seq).where(SyncChangeLog.main_seq.is_not(None))
                     .order_by(SyncChangeLog.main_seq.desc()).limit(1)).scalar() or 0
    for r in rows:
        if r.main_seq is None:
            top += 1
            r.main_seq = top
    db.flush()
    return top


def export_changes(db, since_main_seq=0):
    """Unfiltered accepted rows with main_seq > since, oldest first. New value only, never old_value."""
    if type(since_main_seq) is not int or since_main_seq < 0:
        raise SyncLogError("since must be a non-negative integer")
    rows = db.scalars(select(SyncChangeLog).where(SyncChangeLog.main_seq > since_main_seq)
                      .order_by(SyncChangeLog.main_seq)).all()
    return [{"entity": r.entity, "uid": r.uid, "field": r.field, "op": r.op, "value": r.new_value,
             "main_seq": r.main_seq, "origin_device_id": r.origin_device_id,
             "origin_counter": r.origin_counter, "restore_epoch": r.restore_epoch} for r in rows]


def value_of(change_value, entity, uid, field):
    """Decode an exported value text back to its typed value; refuses a mismatched entity, uid or field."""
    if change_value is None:
        return None
    snap = decode_snapshot(change_value.encode("utf-8"))
    if snap.entity != entity or snap.uid != uid or set(snap.values) != {field}:
        raise SyncLogError("value text does not match its change")
    return snap.values[field]
