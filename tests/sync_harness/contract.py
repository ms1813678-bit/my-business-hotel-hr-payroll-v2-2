"""Wire contract for the three-device sync harness.

This module is the interface the design note (docs/SYNC_DESIGN.md section 3)
describes, written down so the harness can run against a reference engine today
and against the real applier (build order items 3 to 5) without the scenarios
changing. It is test-only: no app/ code imports it.

Shapes
------
Session request (device -> main), POST /sync/session:

    {
      "protocol": 1,
      "session_id": "<uuid, one per attempt>",
      "device_id": "device-b",
      "role": "assistant_manager",
      "epoch": "<restore epoch the device last saw>",   # null when never restored
      "base_counter": 41,                              # last main counter applied
      "changes": [ <change>, ... ]                     # device change log, in seq order
    }

Change:

    {
      "device_id": "device-b",
      "seq": 7,                       # per-device counter, 1, 2, 3, ...
      "op": "create" | "set" | "delete",
      "entity": "employees",
      "uid": "<sync_uid>",
      "field": "phone",               # "" for create/delete
      "old": <json value | null>,     # set: the value the device based its edit on
                                      # delete: sha256 of the record the device saw
      "new": <json value | object>,   # set: the new value; create: field payload
      "local_time": "2026-11-27T21:40:00+02:00",
      "prev_hash": "<hash of the previous change from this device>",
      "hash": "<sha256 over the canonical change body>"
    }

Values are JSON-native: text and ids as strings, numbers as numbers, dates as
"YYYY-MM-DD", timestamps as ISO 8601 with an offset, decimals as strings. The
receiver coerces by the target column type, so a decimal never drifts through
float. A payload carries parent references in wire form (``department_uid``),
never a raw integer id; app.sync_uid owns that mapping.

Session response (main -> device):

    {
      "protocol": 1,
      "status": "ok" | "failed" | "refused_restore" | "refused_role"
                | "refused_chain" | "refused_protocol",
      "message": "<human readable, shown to the user on refusal>",
      "replayed": false,               # true when an identical session_id was already applied
      "main_epoch": "<epoch>",
      "server_counter": 45,            # main's counter after this session
      "applied": [7, 8],               # seq numbers applied
      "duplicate": [3],                # seq numbers the main device already had (retry)
      "conflicts": [ <conflict>, ... ],# seq numbers that became conflicts
      "rejected": [ {"seq": 9, "reason": "field_not_visible"} ],
      "resolved": [ {"seq": 4, "outcome": "accepted" | "rejected",
                     "entity": "employees", "uid": "...", "field": "basic_salary",
                     "main_value": <value>} ],
      "code_corrections": [ {"entity": "employees", "uid": "...", "old": "EMP-0100", "new": "EMP-0101"} ],
      "changes": [ <pull item>, ... ], # main changes since base_counter, filtered by role
      "incomplete": [ {"entity": "employees", "uid": "..."} ]
    }

Pull item:

    {"counter": 44, "op": "set"|"create"|"delete", "entity": "employees",
     "uid": "...", "field": "phone", "new": <value>, "main_time": "<ISO>",
     "main_device": "main"}

Confirm (device -> main), POST /sync/confirm:

    {"protocol": 1, "session_id": "...", "device_id": "device-b", "applied_counter": 45}

Fault injection (test-only, harness -> instance), POST /harness/fault with
{"point": "<one of FAULT_POINTS>"}: the instance arms the point and calls
os._exit(FAULT_EXIT_CODE) when it reaches it. A real applier must expose the
same hook for the interrupted-sync scenarios; without it those scenarios fail
rather than pass silently.

Python entry points the real applier is expected to expose (module
``app.sync_session``, any file layout inside app/):

    apply_session(db, *, device_id, role, epoch, base_counter, changes) -> dict
    pull_changes(db, *, role, since_counter) -> list[dict]
    record_confirmation(db, *, device_id, session_id, applied_counter) -> None
    resolve_conflict(db, *, conflict_id, decision, actor) -> None
    local_change(db, *, device_id, op, entity, uid, field, value) -> None
    device_state(db, *, device_id) -> dict

``db`` is a SQLAlchemy Session bound to that instance's database. The harness
adapts to those names in app_engine.py; when the module is absent the harness
runs its reference engine and says so in the report.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL = 1

OPS = ("create", "set", "delete")

# A delete carries the whole-record hash the deleting device saw, so the main
# device can tell "nobody touched this record since I last saw it" from
# "someone edited it in the meantime" without trusting the device's clock.
DELETE_BASE_FIELD = "record_sha256"

# Interrupted-sync points. Each one kills the process without cleanup, which is
# what a power cut or a network drop looks like to PostgreSQL.
FAULT_POINTS = {
    "main_after_validate": "main validated the session, wrote nothing yet",
    "main_mid_apply": "main wrote part of the session, before commit",
    "main_before_commit": "main wrote every change of the session, before commit",
    "device_before_send": "device built the session, before the request left",
    "device_during_receive": "device sent the session; main may have applied it; reply not read",
    "device_before_confirm": "device applied the reply, before the confirm call",
    "device_mid_apply": "device wrote part of the pull, before commit",
}
FAULT_EXIT_CODE = 17

REFUSAL_STATUSES = ("refused_restore", "refused_role", "refused_chain", "refused_protocol")


class ContractError(ValueError):
    """A payload does not match the documented wire shape."""


def canonical(payload: Any) -> str:
    """Stable text for hashing: sorted keys, no whitespace, unicode kept as is."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def change_body(
    *,
    device_id: str,
    seq: int,
    op: str,
    entity: str,
    uid: str,
    field: str = "",
    old: Any = None,
    new: Any = None,
    local_time: str,
) -> dict:
    """The part of a change the hash covers (everything except prev_hash/hash)."""
    return {
        "device_id": device_id,
        "seq": seq,
        "op": op,
        "entity": entity,
        "uid": uid,
        "field": field,
        "old": old,
        "new": new,
        "local_time": local_time,
    }


def change_hash(prev_hash: str, body: dict) -> str:
    return sha256_text(prev_hash + "\n" + canonical(body))


def make_change(prev_hash: str, **kwargs) -> dict:
    body = change_body(**kwargs)
    return dict(body, prev_hash=prev_hash, hash=change_hash(prev_hash, body))


def verify_chain(changes: list[dict], prev_hash: str) -> None:
    """Raise ContractError when a change list does not hash-chain from prev_hash.

    The chain is tamper evidence per design note section 3: a device cannot
    rewrite or reorder its own history without the main device noticing.
    """
    running = prev_hash or ""
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ContractError(f"change {index} is not an object")
        body = {key: change.get(key) for key in
                ("device_id", "seq", "op", "entity", "uid", "field", "old", "new", "local_time")}
        expected = change_hash(change.get("prev_hash") or "", body)
        if change.get("prev_hash") != running:
            raise ContractError(f"change {index} does not continue the hash chain")
        if change.get("hash") != expected:
            raise ContractError(f"change {index} hash does not match its content")
        running = change["hash"]


def validate_change(raw: Any) -> dict:
    """Shape check only. Policy (role, entity, uid) belongs to the engine."""
    if not isinstance(raw, dict):
        raise ContractError("change is not an object")
    required = {"device_id", "seq", "op", "entity", "uid", "local_time", "prev_hash", "hash"}
    missing = required - set(raw)
    if missing:
        raise ContractError(f"change is missing {sorted(missing)}")
    if raw["op"] not in OPS:
        raise ContractError(f"unknown op {raw['op']!r}")
    if type(raw["seq"]) is not int or raw["seq"] < 1:
        raise ContractError("seq must be a positive integer")
    for key in ("device_id", "entity", "uid", "local_time", "hash"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise ContractError(f"{key} must be a non-empty string")
    if not isinstance(raw.get("prev_hash"), str):
        raise ContractError("prev_hash must be a string (empty for the first change)")
    if raw["op"] == "set":
        if not isinstance(raw.get("field"), str) or not raw["field"]:
            raise ContractError("a set change needs a field")
    elif raw.get("field", "") != "":
        raise ContractError(f"a {raw['op']} change carries no field")
    if raw["op"] == "create" and not isinstance(raw.get("new"), dict):
        raise ContractError("a create change needs a field payload object")
    if raw["op"] == "delete" and raw.get("new") is not None:
        raise ContractError("a delete change carries no new value")
    return raw


def validate_session(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ContractError("session is not an object")
    if raw.get("protocol") != PROTOCOL:
        raise ContractError(f"unsupported protocol {raw.get('protocol')!r}")
    for key in ("session_id", "device_id", "role"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ContractError(f"{key} must be a non-empty string")
    if raw.get("epoch") is not None and not isinstance(raw["epoch"], str):
        raise ContractError("epoch must be a string or null")
    if type(raw.get("base_counter")) is not int or raw["base_counter"] < 0:
        raise ContractError("base_counter must be a non-negative integer")
    changes = raw.get("changes")
    if not isinstance(changes, list):
        raise ContractError("changes must be a list")
    for change in changes:
        validate_change(change)
    if len({change["seq"] for change in changes}) != len(changes):
        raise ContractError("a session carries a seq at most once")
    seqs = [change["seq"] for change in changes]
    if seqs != sorted(seqs):
        raise ContractError("changes must be in seq order")
    return raw


def refusal(status: str, message: str) -> dict:
    """A refusal the device shows to its user. Nothing is applied on either side."""
    if status not in REFUSAL_STATUSES:
        raise ContractError(f"not a refusal status: {status!r}")
    return {
        "protocol": PROTOCOL,
        "status": status,
        "message": message,
        "replayed": False,
        "main_epoch": None,
        "server_counter": None,
        "applied": [],
        "duplicate": [],
        "conflicts": [],
        "rejected": [],
        "resolved": [],
        "code_corrections": [],
        "changes": [],
        "incomplete": [],
    }
