"""Sync apply, part 1b-i: the DECISION step only. Pure: no database, no network, no writes, not wired.

Given one device's incoming session, it decides what the main device would do, without doing it.

Rule 6 as Mo's 27 Nov 22:27 voice note said it, in meaning (docs/SYNC_DESIGN.md section 8): the sync data
is not thrown away. If the session holds a change the device may not make, the WHOLE session stops
(nothing is half-applied), goes back to the administrator who entered it with a warning that names the
offending change, and continues once he removes it. plan_session() therefore returns status "returned"
with that warning and applies nothing.

Other rules (SYNC_DESIGN section 2):
- A duplicate (same device, same counter, same hash) is a no-op.
- A counter gap, a broken hash chain or a counter reuse with other content is an integrity failure:
  ChainBroken is raised, the session is refused as a whole.
- Different records or different fields merge silently. The same field changed on both sides, a delete
  against an edit, or an edit of a record deleted on main, goes to the conflict list. Conflicts do not
  return the session; the other changes keep going.
- Base version: the incoming old value must equal the value on main (after this session's earlier rows),
  otherwise it is a conflict. Time is never used to order changes; the counter is.

Edit rights are NOT decided by anyone yet (a separate rule Mo has not set). validate() in
app/sync_device_export.py only says what a role may SEE, so it must never be used to authorize a write.
may_edit is therefore an explicit argument with no default: a caller that has no edit policy cannot plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field

from app.sync_change_log import GENESIS_HASH, MARKER_FIELD, ChainBroken, SyncLogError, row_hash
from app.sync_device_export import (
    can_write as can_see_and_name,  # read visibility, used only to detect hidden fields
)

READY, RETURNED = "ready", "returned"


@dataclass
class SessionPlan:
    status: str
    apply: list = dc_field(default_factory=list)
    duplicates: list = dc_field(default_factory=list)
    conflicts: list = dc_field(default_factory=list)
    warning: dict | None = None  # set only when status is "returned"


def _g(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _warning(row, reason):
    return {"reason": reason, "entity": _g(row, "entity"), "uid": _g(row, "uid"), "field": _g(row, "field"),
            "origin_device_id": _g(row, "origin_device_id"), "origin_counter": _g(row, "origin_counter")}


def _check_chain(rows, device_id, known):
    """rows ordered by counter; known = {counter: row_hash} already applied here, plus the last applied counter."""
    applied, last = known
    expected_prev = applied.get(last, GENESIS_HASH) if last else GENESIS_HASH
    fresh, dups, nxt = [], [], last + 1
    for r in rows:
        c = _g(r, "origin_counter")
        if _g(r, "origin_device_id") != device_id:
            raise ChainBroken("row from another device in the session")
        want = row_hash(_g(r, "prev_hash"), device_id, c, _g(r, "op"), _g(r, "entity"), _g(r, "uid"), _g(r, "field"),
                        _g(r, "old_value"), _g(r, "new_value"), _g(r, "local_time"), _g(r, "restore_epoch"))
        if want != _g(r, "row_hash"):
            raise ChainBroken("row content does not match its hash")
        if c <= last:
            if applied.get(c) != _g(r, "row_hash"):
                raise ChainBroken("counter already used with other content")
            dups.append(r)
            continue
        if c != nxt or _g(r, "prev_hash") != expected_prev:
            raise ChainBroken("counter gap or broken link")
        fresh.append(r)
        expected_prev, nxt = _g(r, "row_hash"), nxt + 1
    return fresh, dups


def plan_session(*, role, device_id, rows, applied, last_counter, main_values, main_deleted, main_touched, may_edit):
    """Decide a session. Returns a SessionPlan; raises ChainBroken for an integrity failure.

    rows: the device's rows (dict or object) in counter order.
    applied: {counter: row_hash} of this device's rows main already holds; last_counter: highest of them (0 if none).
    main_values: {(entity, uid, field): value text or None} current on main, for the fields the rows touch.
    main_deleted: {(entity, uid)} records deleted on main. main_touched: {(entity, uid)} records main changed
      since this device's base main_seq (used for delete-against-edit).
    may_edit(role, entity, field) -> bool: the edit policy. Required; nobody has decided it yet.
    """
    if not callable(may_edit):
        raise SyncLogError("an edit policy is required")
    fresh, dups = _check_chain(list(rows), device_id, (applied, last_counter))
    for r in fresh:  # whole-session stop comes first: nothing is planned around a bad change
        e, f, op = _g(r, "entity"), _g(r, "field"), _g(r, "op")
        if op == "set" and not can_see_and_name(role, e, f):
            return SessionPlan(RETURNED, warning=_warning(r, "hidden_field"))
        if not may_edit(role, e, MARKER_FIELD if op != "set" else f):
            return SessionPlan(RETURNED, warning=_warning(r, "not_editable"))
    plan, current, session_deleted = SessionPlan(READY, duplicates=dups), dict(main_values), set()
    for r in fresh:
        e, u, f, op = _g(r, "entity"), _g(r, "uid"), _g(r, "field"), _g(r, "op")
        rec = (e, u)
        if op == "create":
            if rec in main_deleted:
                plan.conflicts.append((r, "create_on_deleted"))
            else:
                plan.apply.append(r)
        elif op == "delete":
            if rec in main_touched:
                plan.conflicts.append((r, "delete_against_edit"))
            else:
                plan.apply.append(r)
                session_deleted.add(rec)
        else:
            if rec in main_deleted or rec in session_deleted:
                plan.conflicts.append((r, "edit_on_deleted"))
            elif current.get((e, u, f)) != _g(r, "old_value"):
                plan.conflicts.append((r, "same_field_changed"))
            else:
                plan.apply.append(r)
                current[(e, u, f)] = _g(r, "new_value")
    return plan
