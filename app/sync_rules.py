"""Sync part 1b-ii (pure): three small decisions from docs/SYNC_DESIGN.md, no database, no network, not wired.

- restore_guard (rule 9): may this device sync with this main right now.
- resolve_conflict (rule 3, 4): what the administrator's accept or reject turns into.
- resolve_duplicate_code (rule 7): the code the main gives when an offline device created a code already taken.

Technical shapes only. Every rule's business meaning stays as SYNC_DESIGN states it; the choices marked
"proposal" below are not decided by the owner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

from app.sync_apply_ops import Op
from app.sync_change_log import SyncLogError

# ---- rule 9: restore guard ------------------------------------------------------------------------------

ADMIT = "admit"
REFUSE = "refuse"


@dataclass(frozen=True)
class GuardResult:
    decision: str
    reason: str  # ok | device_epoch_newer | device_counter_ahead | device_epoch_older_decision_needed


def _nonneg(v, label):
    if type(v) is not int or v < 0:
        raise SyncLogError(label + " must be a non-negative integer")
    return v


def restore_guard(*, main_epoch, device_epoch, main_known_counter, device_last_counter):
    """Refuse unless the device and main agree on history. Pure; the refusal text is the caller's.

    main_known_counter: highest counter of this device that main holds. device_last_counter: the highest counter the
    device believes main holds (its last SYNCED counter), NOT its latest local counter: pending unsent rows make the
    local counter legitimately ahead, and using it would refuse every normal sync. A device ahead of main's record, or on a newer epoch, may hold changes main lost in a restore:
    never admitted. A device on an OLDER epoch than main (main was restored after the device last synced) is also
    refused until the administrator decides what happens to its pre-restore edits (SYNC_DESIGN open item; this
    refusal is a proposal, not an owner decision). Checks run in that order, first match wins.
    """
    me, de = _nonneg(main_epoch, "main_epoch"), _nonneg(device_epoch, "device_epoch")
    mk, dl = _nonneg(main_known_counter, "main_known_counter"), _nonneg(device_last_counter, "device_last_counter")
    if de > me:
        return GuardResult(REFUSE, "device_epoch_newer")
    if dl > mk:
        return GuardResult(REFUSE, "device_counter_ahead")
    if de < me:
        return GuardResult(REFUSE, "device_epoch_older_decision_needed")
    return GuardResult(ADMIT, "ok")


# ---- rules 3 and 4: administrator decision on one conflict ----------------------------------------------

ACCEPT, REJECT = "accept", "reject"
_ACCEPTABLE = {"same_field_changed": "set", "delete_against_edit": "delete"}


def resolve_conflict(conflict, decision):
    """conflict: one dict from SessionOps.conflicts. Returns a tuple of Ops to apply (empty for reject).

    accept of same_field_changed -> the device's set (its new value replaces main's); accept of delete_against_edit ->
    the delete. reject -> nothing, the main value stays. accept of edit_on_deleted or create_on_deleted is REFUSED:
    SYNC_DESIGN rule 4 says nothing is deleted silently but defines no undelete, so that needs a rule first.
    "Confirm both" (finance salary with effective dates) is not implemented here.
    A malformed conflict raises SyncLogError, never KeyError or AttributeError.
    Executor note: the accepted set still carries the device's old_value while main holds another value; the executor
    must apply it as decided and must not re-flag it as a new conflict.
    """
    if decision not in (ACCEPT, REJECT):
        raise SyncLogError("decision must be accept or reject")
    if not isinstance(conflict, dict):
        raise SyncLogError("conflict must be a dict")
    reason = conflict.get("reason")
    if reason not in ("same_field_changed", "delete_against_edit", "edit_on_deleted", "create_on_deleted"):
        raise SyncLogError("unknown conflict reason")
    if decision == REJECT:
        return ()
    kind = _ACCEPTABLE.get(reason)
    if kind is None:
        raise SyncLogError("accepting this conflict needs a rule that does not exist yet")
    fields = ("kind", "entity", "uid", "field", "old_value", "new_value", "origin_device_id", "origin_counter")
    if any(k not in conflict for k in fields) or conflict["kind"] != kind:
        raise SyncLogError("conflict is malformed or does not match its reason")
    if not all(isinstance(conflict[k], str) for k in ("entity", "uid", "field", "origin_device_id")) or any(
            conflict[k] is not None and not isinstance(conflict[k], str) for k in ("old_value", "new_value")) or (
            type(conflict["origin_counter"]) is not int or conflict["origin_counter"] < 1):
        raise SyncLogError("conflict is malformed")
    return (Op(**{k: conflict[k] for k in fields}),)


# ---- rule 7: duplicate employee code ----------------------------------------------------------------------

_CODE = re.compile(r"^MY-(\d{4,})$")


def _number(code):
    m = _CODE.match(code) if isinstance(code, str) else None
    if not m:
        raise SyncLogError("employee code must look like MY-0001")
    return int(m.group(1))


class CodeResult(NamedTuple):
    code: str
    corrected: bool  # True when the code differs from the one the device asked for
    counter_after: int  # the real counter must be at least this; the caller advances it in the same transaction


def resolve_duplicate_code(wanted, codes_in_use, counter_last_number):
    """Rule 7. Codes compare by NUMBER, so MY-00007 and MY-0007 are the same code. A free wanted code is kept
    (written canonically, MY-NNNN at least four digits; a non-canonical spelling such as MY-00007 is therefore
    reported with corrected=True, which only means the text changed, not that the number did) and, when its number is above the counter, the counter must
    advance to it (counter_after), otherwise the allocator would later hand the same code out again. A taken code
    becomes the next number after the highest of the counter and every code in use. This function reserves nothing:
    the caller advances the real counter to counter_after in the same transaction."""
    last = _nonneg(counter_last_number, "counter_last_number")
    want = _number(wanted)
    if isinstance(codes_in_use, (str, bytes)) or not hasattr(codes_in_use, "__iter__"):
        raise SyncLogError("codes_in_use must be a collection of codes")
    taken = {_number(c) for c in codes_in_use}
    if want not in taken:
        code = f"MY-{want:04d}"
        return CodeResult(code, code != wanted, max(last, want))
    new = max([last] + list(taken)) + 1
    return CodeResult(f"MY-{new:04d}", True, new)
