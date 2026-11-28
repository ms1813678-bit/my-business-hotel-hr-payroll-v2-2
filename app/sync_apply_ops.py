"""Sync apply, part 1b-ii (pure): turn a SessionPlan into an ordered list of operations and records.

No database, no network, no writes, not wired. A later part runs the result in ONE transaction; if any op
fails that transaction rolls back and nothing of the session stays (rule 6). This module only fixes the
shape and the order, so that shape can be reviewed and tested without a database.

- plan_to_ops(plan): READY plan -> SessionOps. Ops are in counter order. Every row the session consumed
  (applied, conflicted, already-deleted) is covered by `last_counter`, so a retry sees the same rows as
  duplicates and changes nothing. Conflicts become conflict records for the administrator's list; they keep
  both values because that list exists to show them to the main administrator only.
- returned_record(plan, ...): RETURNED plan -> the record kept for the administrator's warning. It carries
  no field values: only the offending change's names, a session digest and times.
- is_expired(record, now, days): days is a required argument; the retention period is not decided.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timedelta, timezone

from app.sync_apply import READY, RETURNED, SessionPlan, _g
from app.sync_change_log import SyncLogError, canonical_time

KIND = {"create": "create", "set": "set", "delete": "delete"}


@dataclass(frozen=True)
class Op:
    kind: str
    entity: str
    uid: str
    field: str
    old_value: str | None
    new_value: str | None
    origin_device_id: str
    origin_counter: int


@dataclass(frozen=True)
class SessionOps:
    device_id: str
    ops: tuple
    conflicts: tuple  # dicts: reason + Op fields
    already_deleted: tuple  # Ops that needed no change
    duplicates: int
    last_counter: int  # highest counter this session consumed or already held
    session_digest: str
    received_at: str | None = None  # rule 10: when main received this session (canonical UTC), None if not stamped


def _op(row):
    kind = KIND.get(_g(row, "op"))
    if kind is None:
        raise SyncLogError("unknown operation")
    return Op(kind, _g(row, "entity"), _g(row, "uid"), _g(row, "field"), _g(row, "old_value"), _g(row, "new_value"),
              _g(row, "origin_device_id"), _g(row, "origin_counter"))


def digest(rows):
    """Order-independent digest of a session: SHA-256 over its row hashes in counter order."""
    ordered = sorted(rows, key=lambda r: _g(r, "origin_counter"))
    return hashlib.sha256("\n".join(_g(r, "row_hash") for r in ordered).encode("ascii")).hexdigest()


def plan_to_ops(plan: SessionPlan, *, device_id, applied_last_counter=0, received_at=None):
    if not isinstance(plan, SessionPlan) or plan.status != READY:
        raise SyncLogError("only a ready plan can be turned into operations")
    ops = tuple(sorted((_op(r) for r in plan.apply), key=lambda o: o.origin_counter))
    already = tuple(sorted((_op(r) for r in plan.already_deleted), key=lambda o: o.origin_counter))
    conflicts = tuple(
        {"reason": reason, **_op(r).__dict__}
        for r, reason in sorted(plan.conflicts, key=lambda c: _g(c[0], "origin_counter")))
    consumed = [o.origin_counter for o in ops] + [o.origin_counter for o in already] + [c["origin_counter"] for c in conflicts]
    last = max([applied_last_counter] + consumed)
    seen = {o.origin_counter for o in ops} | {o.origin_counter for o in already} | {c["origin_counter"] for c in conflicts}
    if len(seen) != len(consumed):
        raise SyncLogError("a counter appears in more than one outcome")
    if any(x.origin_device_id != device_id for x in list(ops) + list(already)) or any(
            c["origin_device_id"] != device_id for c in conflicts):
        raise SyncLogError("row from another device in the plan")
    allrows = list(plan.apply) + list(plan.already_deleted) + [c[0] for c in plan.conflicts] + list(plan.duplicates)
    stamp = None if received_at is None else canonical_time(received_at)  # naive time refused
    return SessionOps(device_id, ops, conflicts, already, len(plan.duplicates), last, digest(allrows), stamp)


def returned_record(plan: SessionPlan, *, device_id, rows, received_at):
    """Record for a returned session. Names only, never values. received_at must be timezone-aware."""
    if not isinstance(plan, SessionPlan) or plan.status != RETURNED or not plan.warning:
        raise SyncLogError("only a returned plan has a returned record")
    rows = list(rows)
    if not isinstance(received_at, datetime) or received_at.tzinfo is None:
        raise SyncLogError("received_at must be timezone-aware")
    return {
        "status": "returned",
        "device_id": device_id,
        "session_digest": digest(rows),
        "row_count": len(rows),
        "warning": dict(plan.warning),
        "received_at": received_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def is_expired(record, now, days):
    """True when a returned record is older than `days` days. `days` has no default (retention not decided)."""
    if type(days) is not int or days < 1:
        raise SyncLogError("days must be a positive integer")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise SyncLogError("now must be timezone-aware")
    at = datetime.strptime(record["received_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) - at > timedelta(days=days)
