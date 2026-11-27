"""Reference implementation of the sync contract (test double, not product code).

The real applier is build order item 3 to 5 (docs/SYNC_DESIGN.md section 6) and
does not exist yet. This module implements the same contract against the real
tables, the real sync_uid mapping, the real per-role column policy and the real
restore epoch, so the harness scenarios can be written, reviewed and run now.
When app.sync_session lands, the harness runs against it instead (see
app_engine.py) and this file becomes the oracle the scenarios were proven on.

Design decisions taken from the note, and where this file has to interpret it:

* Merge is base-compare on the field: a change applies when the value the device
  edited from is still the main device's value, so two devices editing different
  fields of one record both apply (rules 2) and the same field with a stale base
  is a conflict (rule 3). Values compare on their wire form, never on device
  clocks (rule 10).
* A delete carries the whole-record hash the device saw. A record that changed
  since is a conflict, never a silent delete (rule 4).
* One transaction per session on the main device. A policy rejection (a hidden
  field) is recorded and alerted, and the rest of the session still applies; a
  malformed session, a broken hash chain or an epoch mismatch refuses the whole
  session and applies nothing (rule 5, 6, 9).
* Restore epoch is the product's own SystemSetting('restore_epoch') read through
  app.restore_lock. A device that last saw a different epoch is refused until the
  administrator re-baselines it; re-baselining keeps the device's unsent edits as
  conflicts instead of dropping them.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app import sync_visibility
from app.restore_lock import read_restore_epoch
from app.sync_uid import ENTITIES as SYNC_ENTITIES
from app.sync_uid import canonical_uid

from . import contract, store


class EngineError(RuntimeError):
    """The session cannot run at all; nothing was applied."""


def read_epoch(connection: Connection) -> str | None:
    """The product's restore epoch, read through the ORM helper it lives behind."""
    from sqlalchemy.orm import Session

    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        return read_restore_epoch(session)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unjson(text_value: str | None) -> Any:
    return None if text_value is None else json.loads(text_value)


def _check_uid(uid: str) -> str:
    canonical_uid(uid)
    return uid


# ---------------------------------------------------------------- device rows


def pair_device(connection: Connection, device_id: str, role: str, *, epoch: str | None) -> None:
    """Register a device with the role its credential carries (pairing code, rule 5)."""
    if role not in sync_visibility.ROLES:
        raise EngineError(f"unknown role {role!r}")
    connection.execute(
        text("INSERT INTO harness_sync_device(device_id, role, epoch_seen) "
             "VALUES (:d, :r, :e) ON CONFLICT (device_id) DO UPDATE "
             "SET role = EXCLUDED.role, epoch_seen = EXCLUDED.epoch_seen"),
        {"d": device_id, "r": role, "e": epoch},
    )


def device_row(connection: Connection, device_id: str):
    return connection.execute(
        text("SELECT * FROM harness_sync_device WHERE device_id = :d"), {"d": device_id}
    ).mappings().first()


def device_state(connection: Connection, device_id: str) -> dict:
    row = device_row(connection, device_id)
    if row is None:
        raise EngineError(f"unknown device {device_id!r}")
    return dict(row)


# ---------------------------------------------------------------- main side


def _log(connection: Connection, *, device_id: str, seq: int, op: str, entity: str, uid: str,
         field: str, old: Any, new: Any, local_time: dt.datetime, counter: int, status: str,
         session_id: str | None = None, prev_hash: str = "", change_hash: str = "") -> None:
    connection.execute(
        text("""INSERT INTO harness_sync_change(device_id, seq, op, entity, uid, field, old_value,
                 new_value, local_time, received_at, counter, status, session_id, prev_hash, hash)
                VALUES (:device_id, :seq, :op, :entity, :uid, :field, :old, :new, :local_time,
                        now(), :counter, :status, :session_id, :prev_hash, :hash)"""),
        {"device_id": device_id, "seq": seq, "op": op, "entity": entity, "uid": uid, "field": field,
         "old": _json(old), "new": _json(new), "local_time": local_time, "counter": counter,
         "status": status, "session_id": session_id, "prev_hash": prev_hash, "hash": change_hash},
    )


def _record_main_change(connection: Connection, *, op: str, entity: str, uid: str, field: str,
                        old: Any, new: Any, actor: str = "main") -> int:
    """A change made on the main device itself (an administrator's screen edit)."""
    counter = store.next_counter(connection)
    _log(connection, device_id=actor, seq=counter, op=op, entity=entity, uid=uid, field=field,
         old=old, new=new, local_time=utcnow(), counter=counter, status="applied")
    return counter


def main_local_change(connection: Connection, *, op: str, entity: str, uid: str,
                      field: str = "", value: Any = None, actor: str = "main") -> dict:
    """Apply one administrator edit on the main device and broadcast it."""
    if entity not in SYNC_ENTITIES:
        raise EngineError(f"entity outside sync scope: {entity!r}")
    _check_uid(uid)
    table = store.entity_table(entity)
    row = store.row_for_uid(connection, entity, uid)
    if op == "create":
        if row is not None:
            raise EngineError("record already exists on the main device")
        payload = dict(value or {})
        _reject_hidden_payload(entity, payload, sync_visibility.ROLE_ADMIN)
        store.insert_row(connection, entity, uid, payload, role=sync_visibility.ROLE_ADMIN)
        counter = _record_main_change(connection, op="create", entity=entity, uid=uid, field="",
                                      old=None, new=payload, actor=actor)
        return {"counter": counter, "op": op, "uid": uid}
    if row is None:
        raise EngineError("record does not exist on the main device")
    if op == "delete":
        before = store.row_payload(connection, entity, row, sync_visibility.ROLE_ADMIN)
        store.delete_row(connection, entity, uid)
        counter = _record_main_change(connection, op="delete", entity=entity, uid=uid, field="",
                                      old=None, new=None, actor=actor)
        return {"counter": counter, "op": op, "uid": uid, "deleted": before}
    if op != "set":
        raise EngineError(f"unknown op {op!r}")
    before = store.row_payload(connection, entity, row, sync_visibility.ROLE_ADMIN).get(field)
    store.update_field(connection, entity, uid, field, value)
    counter = _record_main_change(connection, op="set", entity=entity, uid=uid, field=field,
                                  old=before, new=value, actor=actor)
    return {"counter": counter, "op": op, "uid": uid, "field": field, "old": before, "new": value}


def _reject_hidden_payload(entity: str, payload: dict, role: str) -> list[str]:
    """Fields a role may not write. Empty list means the payload is acceptable."""
    allowed = sync_visibility.visible_columns(entity, role)
    forbidden = []
    for key in payload:
        column = key[:-4] + "_id" if key.endswith("_uid") else key
        if key == "branch_name":
            forbidden.append(key)
            continue
        if column not in store.entity_table(entity).c:
            forbidden.append(key)
            continue
        if column not in allowed:
            forbidden.append(key)
    return forbidden


def _last_writer(connection: Connection, entity: str, uid: str, field: str) -> dict:
    row = connection.execute(
        text("""SELECT device_id, received_at FROM harness_sync_change
                WHERE entity = :e AND uid = :u AND field = :f AND status = 'applied'
                ORDER BY counter DESC LIMIT 1"""),
        {"e": entity, "u": uid, "f": field},
    ).mappings().first()
    return dict(row) if row else {"device_id": None, "received_at": None}


def _alert(connection: Connection, *, kind: str, device_id: str, entity: str, uid: str,
           field: str, detail: str) -> None:
    connection.execute(
        text("""INSERT INTO harness_sync_alert(kind, device_id, entity, uid, field, detail)
                VALUES (:kind, :device_id, :entity, :uid, :field, :detail)"""),
        {"kind": kind, "device_id": device_id, "entity": entity, "uid": uid, "field": field,
         "detail": detail},
    )


def _conflict(connection: Connection, *, kind: str, entity: str, uid: str, field: str,
              device_id: str, device_seq: int, device_value: Any, device_time: str | None,
              main_value: Any, main_time: dt.datetime | None, main_device: str | None,
              session_id: str) -> int:
    row = connection.execute(
        text("""INSERT INTO harness_sync_conflict(kind, entity, uid, field, device_id, device_seq,
                 device_value, device_time, main_value, main_time, main_device, session_id)
                VALUES (:kind, :entity, :uid, :field, :device_id, :device_seq, :device_value,
                        :device_time, :main_value, :main_time, :main_device, :session_id)
                RETURNING id"""),
        {"kind": kind, "entity": entity, "uid": uid, "field": field, "device_id": device_id,
         "device_seq": device_seq, "device_value": _json(device_value),
         "device_time": device_time, "main_value": _json(main_value), "main_time": main_time,
         "main_device": main_device, "session_id": session_id},
    ).scalar()
    return int(row)


def _replace_pending_conflict(connection: Connection, *, entity: str, uid: str, field: str,
                              device_id: str, device_seq: int, device_value: Any,
                              device_time: str | None, main_value: Any, main_time: dt.datetime | None,
                              main_device: str | None, session_id: str,
                              kind: str = "same_field") -> int:
    """A newer edit from the same device replaces its queued conflict."""
    row = connection.execute(
        text("""UPDATE harness_sync_conflict
                SET device_seq = :device_seq, device_value = :device_value,
                    device_time = :device_time, main_value = :main_value, main_time = :main_time,
                    main_device = :main_device, session_id = :session_id, created_at = now()
                WHERE entity = :entity AND uid = :uid AND field = :field
                  AND device_id = :device_id AND status = 'pending' AND kind = :kind
                RETURNING id"""),
        {"kind": kind, "entity": entity, "uid": uid, "field": field, "device_id": device_id,
         "device_seq": device_seq, "device_value": _json(device_value), "device_time": device_time,
         "main_value": _json(main_value), "main_time": main_time, "main_device": main_device,
         "session_id": session_id},
    ).scalar()
    if row is None:
        return _conflict(connection, kind=kind, entity=entity, uid=uid, field=field,
                         device_id=device_id, device_seq=device_seq, device_value=device_value,
                         device_time=device_time, main_value=main_value, main_time=main_time,
                         main_device=main_device, session_id=session_id)
    return int(row)


def apply_session(connection: Connection, request: dict, *, session_id: str,
                  fault=None) -> dict:
    """One session, one transaction. The caller commits or rolls back.

    Raises EngineError only for a refusal that must apply nothing.
    """
    device = device_row(connection, request["device_id"])
    if device is None:
        return contract.refusal("refused_role", "This device is not paired with the main device.")
    if device["role"] != request["role"]:
        return contract.refusal("refused_role", "The device role does not match its pairing.")
    main_epoch = read_epoch(connection)
    if (request.get("epoch") or None) != (main_epoch or None):
        # Remember how far the device actually got (its chain head), so a later
        # administrator re-baseline can continue the device's hash chain instead of
        # pretending its history never existed. No business row, counter or change
        # log entry is touched here.
        if request["changes"]:
            head = request["changes"][-1]
            connection.execute(
                text("UPDATE harness_sync_device SET last_seq = :seq, last_hash = :hash "
                     "WHERE device_id = :d"),
                {"seq": head["seq"], "hash": head["hash"], "d": request["device_id"]},
            )
        return contract.refusal(
            "refused_restore",
            "The main device's database was restored from a backup. Sync is refused until the "
            "administrator decides which side is current.",
        )
    existing = connection.execute(
        text("SELECT request_sha256, response_json FROM harness_sync_session WHERE session_id = :s"),
        {"s": session_id},
    ).mappings().first()
    request_sha = contract.sha256_text(contract.canonical(request))
    if existing is not None:
        if existing["request_sha256"] != request_sha:
            return contract.refusal("refused_protocol",
                                    "This session id was already used for a different session.")
        response = json.loads(existing["response_json"])
        response["replayed"] = True
        return response

    last_seq = int(device["last_seq"])
    fresh = [c for c in request["changes"] if c["seq"] > last_seq]
    try:
        contract.verify_chain(fresh, device["last_hash"] or "")
    except contract.ContractError as exc:
        return contract.refusal("refused_chain", f"The device's change history is not continuous: {exc}")
    applied: list[int] = []
    duplicate: list[int] = []
    conflicts: list[dict] = []
    rejected: list[dict] = []
    corrections: list[dict] = []

    for change in request["changes"]:
        seq = change["seq"]
        if seq <= last_seq:
            duplicate.append(seq)
            continue
        outcome = _apply_one(connection, change, session_id=session_id, corrections=corrections)
        if fault is not None and len(request["changes"]) > 1 and \
                len(applied) + len(conflicts) + len(rejected) == len(request["changes"]) // 2:
            fault("main_mid_apply")
        if outcome["result"] == "applied":
            applied.append(seq)
        elif outcome["result"] == "conflict":
            conflicts.append({"seq": seq, "conflict_id": outcome["conflict_id"],
                              "kind": outcome["kind"], "entity": change["entity"],
                              "uid": change["uid"], "field": change.get("field", "")})
        else:
            rejected.append({"seq": seq, "reason": outcome["reason"]})

    if request["changes"]:
        last = request["changes"][-1]
        connection.execute(
            text("UPDATE harness_sync_device SET last_seq = :seq, last_hash = :hash WHERE device_id = :d"),
            {"seq": last["seq"], "hash": last["hash"], "d": request["device_id"]},
        )

    resolved = _collect_resolutions(connection, request["device_id"])
    response = {
        "protocol": contract.PROTOCOL,
        "status": "ok",
        "message": "",
        "replayed": False,
        "main_epoch": main_epoch,
        "server_counter": store.current_counter(connection),
        "applied": applied,
        "duplicate": duplicate,
        "conflicts": conflicts,
        "rejected": rejected,
        "resolved": resolved,
        "code_corrections": corrections,
        "changes": pull_items(connection, role=request["role"],
                              since_counter=int(request["base_counter"])),
        "incomplete": _incomplete(connection),
    }
    connection.execute(
        text("""INSERT INTO harness_sync_session(session_id, device_id, request_sha256, response_json)
                VALUES (:s, :d, :sha, :json)"""),
        {"s": session_id, "d": request["device_id"], "sha": request_sha,
         "json": json.dumps(response, sort_keys=True)},
    )
    return response


def _apply_one(connection: Connection, change: dict, *, session_id: str,
               corrections: list[dict]) -> dict:
    entity, uid, op = change["entity"], change["uid"], change["op"]
    device_id = change["device_id"]
    if entity not in SYNC_ENTITIES:
        _alert(connection, kind="unknown_entity", device_id=device_id, entity=str(entity), uid=str(uid),
               field="", detail="change names an entity outside the sync scope")
        return {"result": "rejected", "reason": "unknown_entity"}
    try:
        _check_uid(uid)
    except Exception:
        _alert(connection, kind="malformed_uid", device_id=device_id, entity=entity, uid=str(uid),
               field="", detail="uid is not canonical")
        return {"result": "rejected", "reason": "malformed_uid"}
    role = device_row(connection, device_id)["role"]

    if op == "create":
        return _apply_create(connection, change, role=role, session_id=session_id,
                             corrections=corrections)
    if op == "delete":
        return _apply_delete(connection, change, role=role, session_id=session_id)
    return _apply_set(connection, change, role=role, session_id=session_id)


def _apply_create(connection: Connection, change: dict, *, role: str, session_id: str,
                  corrections: list[dict]) -> dict:
    entity, uid = change["entity"], change["uid"]
    payload = dict(change["new"])
    forbidden = _reject_hidden_payload(entity, payload, role)
    if forbidden:
        _alert(connection, kind="hidden_field_edit", device_id=change["device_id"], entity=entity,
               uid=uid, field=",".join(sorted(forbidden)),
               detail="a device sent fields its role may not see; the record was not created")
        return {"result": "rejected", "reason": "field_not_visible"}
    if store.row_for_uid(connection, entity, uid) is not None:
        return {"result": "rejected", "reason": "duplicate_uid"}
    if entity == "employees" and isinstance(payload.get("code"), str):
        if store.code_in_use(connection, payload["code"]):
            new_code = store.next_employee_code(connection, payload["code"])
            corrections.append({"entity": entity, "uid": uid, "old": payload["code"], "new": new_code})
            payload["code"] = new_code
    missing_required = _missing_required(entity, payload)
    store.insert_row(connection, entity, uid, payload, role=role)
    counter = store.next_counter(connection)
    _log(connection, device_id=change["device_id"], seq=change["seq"], op="create", entity=entity,
         uid=uid, field="", old=None, new=payload, local_time=_local_time(change), counter=counter,
         status="applied", session_id=session_id, prev_hash=change["prev_hash"], change_hash=change["hash"])
    if missing_required:
        connection.execute(
            text("""INSERT INTO harness_sync_incomplete(entity, uid, device_id) VALUES (:e, :u, :d)
                    ON CONFLICT (entity, uid) DO NOTHING"""),
            {"e": entity, "u": uid, "d": change["device_id"]},
        )
        _alert(connection, kind="incomplete_employee", device_id=change["device_id"], entity=entity,
               uid=uid, field=",".join(sorted(missing_required)),
               detail="created on a device that cannot see required fields; the administrator "
                      "must complete it")
    return {"result": "applied"}


def _missing_required(entity: str, payload: dict) -> list[str]:
    """NOT NULL columns without a value in the payload.

    A record created without them carries placeholders and waits as incomplete
    for the administrator (rule 8), whether the device could see the column or
    simply left it out.
    """
    table = store.entity_table(entity)
    missing = []
    for column in table.c:
        if column.name in ("id", "sync_uid") or column.name in store.NEVER_SYNCED:
            continue
        if column.nullable or column.default is not None or column.server_default is not None:
            continue
        if payload.get(store.wire_key(entity, column.name)) is None:
            missing.append(column.name)
    return missing


def _apply_set(connection: Connection, change: dict, *, role: str, session_id: str) -> dict:
    entity, uid, field = change["entity"], change["uid"], change["field"]
    allowed = sync_visibility.visible_columns(entity, role)
    column = field[:-4] + "_id" if field.endswith("_uid") else field
    if field == "branch_name" or column not in store.entity_table(entity).c or column not in allowed:
        _alert(connection, kind="hidden_field_edit", device_id=change["device_id"], entity=entity,
               uid=uid, field=field,
               detail="a device sent a field its role may not write; the change was rejected")
        return {"result": "rejected", "reason": "field_not_visible"}
    row = store.row_for_uid(connection, entity, uid)
    if row is None:
        if _has_tombstone(connection, entity, uid):
            return _make_conflict(connection, change, kind="edit_vs_delete", main_value=None,
                                  main_time=None, main_device=None, session_id=session_id)
        return {"result": "rejected", "reason": "unknown_uid"}
    current = store.row_payload(connection, entity, row, sync_visibility.ROLE_ADMIN).get(field)
    if current == change.get("new"):
        return {"result": "applied"}
    if current != change.get("old"):
        writer = _last_writer(connection, entity, uid, field)
        return _make_conflict(connection, change, kind="same_field", main_value=current,
                              main_time=writer["received_at"], main_device=writer["device_id"],
                              session_id=session_id)
    store.update_field(connection, entity, uid, field, change["new"])
    counter = store.next_counter(connection)
    _log(connection, device_id=change["device_id"], seq=change["seq"], op="set", entity=entity,
         uid=uid, field=field, old=change.get("old"), new=change.get("new"),
         local_time=_local_time(change), counter=counter, status="applied", session_id=session_id,
         prev_hash=change["prev_hash"], change_hash=change["hash"])
    return {"result": "applied"}


def _apply_delete(connection: Connection, change: dict, *, role: str, session_id: str) -> dict:
    entity, uid = change["entity"], change["uid"]
    row = store.row_for_uid(connection, entity, uid)
    if row is None:
        return {"result": "applied"}
    current = store.record_sha256(connection, entity, uid, role)
    if change.get("old") != current:
        writer = _last_writer(connection, entity, uid, "")
        return _make_conflict(connection, change, kind="delete_vs_edit",
                              main_value=store.row_payload(connection, entity, row,
                                                           sync_visibility.ROLE_ADMIN),
                              main_time=writer["received_at"], main_device=writer["device_id"],
                              session_id=session_id)
    store.delete_row(connection, entity, uid)
    counter = store.next_counter(connection)
    _log(connection, device_id=change["device_id"], seq=change["seq"], op="delete", entity=entity,
         uid=uid, field="", old=None, new=None, local_time=_local_time(change), counter=counter,
         status="applied", session_id=session_id, prev_hash=change["prev_hash"],
         change_hash=change["hash"])
    return {"result": "applied"}


def _make_conflict(connection: Connection, change: dict, *, kind: str, main_value: Any,
                   main_time, main_device, session_id: str) -> dict:
    field = change.get("field", "")
    writer = _last_writer(connection, change["entity"], change["uid"], field) if field else {
        "device_id": main_device, "received_at": main_time}
    conflict_id = _replace_pending_conflict(
        connection, entity=change["entity"], uid=change["uid"], field=field,
        device_id=change["device_id"], device_seq=change["seq"], device_value=change.get("new"),
        device_time=change.get("local_time"), main_value=main_value,
        main_time=writer["received_at"], main_device=writer["device_id"], session_id=session_id,
        kind=kind,
    )
    return {"result": "conflict", "conflict_id": conflict_id, "kind": kind}


def _local_time(change: dict) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(str(change["local_time"]))
    except Exception:
        return utcnow()


def _has_tombstone(connection: Connection, entity: str, uid: str) -> bool:
    return connection.execute(
        text("""SELECT 1 FROM harness_sync_change WHERE entity = :e AND uid = :u
                AND op = 'delete' AND status = 'applied' LIMIT 1"""),
        {"e": entity, "u": uid},
    ).first() is not None


def _incomplete(connection: Connection) -> list[dict]:
    return [dict(row) for row in connection.execute(
        text("SELECT entity, uid, device_id, status FROM harness_sync_incomplete "
             "WHERE status = 'pending' ORDER BY uid")
    ).mappings()]


def _collect_resolutions(connection: Connection, device_id: str) -> list[dict]:
    rows = connection.execute(
        text("""SELECT id, device_seq, status, kind, entity, uid, field, main_value
                FROM harness_sync_conflict
                WHERE device_id = :d AND status <> 'pending' AND notified_at IS NULL
                ORDER BY id"""),
        {"d": device_id},
    ).mappings().all()
    out = []
    for row in rows:
        connection.execute(text("UPDATE harness_sync_conflict SET notified_at = now() WHERE id = :i"),
                           {"i": row["id"]})
        out.append({"seq": row["device_seq"], "outcome": row["status"], "kind": row["kind"],
                    "entity": row["entity"], "uid": row["uid"], "field": row["field"],
                    "main_value": _unjson(row["main_value"])})
    return out


# ---------------------------------------------------------------- pull


def pull_items(connection: Connection, *, role: str, since_counter: int) -> list[dict]:
    """Everything this role may see after since_counter, oldest first."""
    items: list[dict] = []
    if since_counter <= 0:
        for entity in store.BUSINESS_ENTITIES:
            table = store.entity_table(entity)
            for row in connection.execute(table.select()).mappings().all():
                payload = store.row_payload(connection, entity, row, role)
                if not payload:
                    continue
                items.append({"counter": 0, "op": "create", "entity": entity,
                              "uid": row["sync_uid"], "field": "", "new": payload,
                              "main_time": None, "main_device": "main"})
    log = connection.execute(
        text("""SELECT counter, op, entity, uid, field, new_value, received_at, device_id
                FROM harness_sync_change WHERE status = 'applied' AND counter > :since
                ORDER BY counter"""),
        {"since": max(since_counter, 0)},
    ).mappings().all()
    for entry in log:
        entity = entry["entity"]
        if entity not in SYNC_ENTITIES:
            continue
        if entry["op"] == "set":
            column = entry["field"][:-4] + "_id" if entry["field"].endswith("_uid") else entry["field"]
            if column not in sync_visibility.visible_columns(entity, role):
                continue
            items.append({"counter": entry["counter"], "op": "set", "entity": entity,
                          "uid": entry["uid"], "field": entry["field"],
                          "new": _unjson(entry["new_value"]),
                          "main_time": iso(entry["received_at"]), "main_device": entry["device_id"]})
        elif entry["op"] == "delete":
            items.append({"counter": entry["counter"], "op": "delete", "entity": entity,
                          "uid": entry["uid"], "field": "", "new": None,
                          "main_time": iso(entry["received_at"]), "main_device": entry["device_id"]})
        elif entry["op"] == "create":
            row = store.row_for_uid(connection, entity, entry["uid"])
            if row is None:
                continue
            payload = store.row_payload(connection, entity, row, role)
            if not payload:
                continue
            items.append({"counter": entry["counter"], "op": "create", "entity": entity,
                          "uid": entry["uid"], "field": "", "new": payload,
                          "main_time": iso(entry["received_at"]), "main_device": entry["device_id"]})
    return items


def confirm_session(connection: Connection, *, session_id: str, device_id: str,
                    applied_counter: int) -> None:
    connection.execute(
        text("""UPDATE harness_sync_session SET confirmed_at = now()
                WHERE session_id = :s AND device_id = :d"""),
        {"s": session_id, "d": device_id},
    )
    connection.execute(
        text("""UPDATE harness_sync_device SET last_confirmed_counter = :c WHERE device_id = :d"""),
        {"c": applied_counter, "d": device_id},
    )


# ---------------------------------------------------------------- conflicts


def resolve_conflict(connection: Connection, *, conflict_id: int, decision: str,
                     actor: str = "admin") -> dict:
    """The administrator's accept or reject on the main device (rule 3)."""
    if decision not in ("accept", "reject"):
        raise EngineError("decision must be accept or reject")
    row = connection.execute(
        text("SELECT * FROM harness_sync_conflict WHERE id = :i"), {"i": conflict_id}
    ).mappings().first()
    if row is None:
        raise EngineError("no such conflict")
    if row["status"] != "pending":
        raise EngineError(f"conflict already {row['status']}")
    value = _unjson(row["device_value"])
    if decision == "accept":
        if row["kind"] == "delete_vs_edit":
            current = store.record_sha256(connection, row["entity"], row["uid"], "system_admin")
            store.delete_row(connection, row["entity"], row["uid"])
            counter = _record_main_change(connection, op="delete", entity=row["entity"],
                                          uid=row["uid"], field="", old=None, new=None, actor=actor)
        elif row["field"]:
            current = store.row_for_uid(connection, row["entity"], row["uid"])
            before = None if current is None else store.row_payload(
                connection, row["entity"], current, sync_visibility.ROLE_ADMIN).get(row["field"])
            store.update_field(connection, row["entity"], row["uid"], row["field"], value)
            counter = _record_main_change(connection, op="set", entity=row["entity"],
                                          uid=row["uid"], field=row["field"], old=before,
                                          new=value, actor=actor)
        else:
            raise EngineError("cannot accept a conflict without a field")
    else:
        counter = None
    connection.execute(
        text("""UPDATE harness_sync_conflict SET status = :status, resolved_by = :actor,
                resolved_at = now(), notified_at = NULL WHERE id = :i"""),
        {"status": decision + "ed", "actor": actor, "i": conflict_id},
    )
    return {"conflict_id": conflict_id, "decision": decision, "counter": counter}


def rebaseline_device(connection: Connection, *, device_id: str) -> dict:
    """Administrator decision after a restore: the main device is the truth.

    Only the main device's record of the device changes here. The device keeps
    its own unsent edits on its own disk (device_engine.rebaseline_local turns
    them into local conflicts), so nothing is silently lost and nothing
    overwrites the restored database.
    """
    device = device_row(connection, device_id)
    if device is None:
        raise EngineError("unknown device")
    connection.execute(
        text("""UPDATE harness_sync_device SET last_seq = 0,
                last_confirmed_counter = 0, epoch_seen = :epoch WHERE device_id = :d"""),
        {"epoch": read_epoch(connection), "d": device_id},
    )
    return {"device_id": device_id, "epoch": read_epoch(connection)}


def _has_table(connection: Connection, name: str) -> bool:
    return connection.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar() is not None


def complete_employee(connection: Connection, *, uid: str, values: dict,
                      actor: str = "admin") -> dict:
    """The administrator fills in the fields an assistant-created record lacks (rule 8)."""
    for field, value in values.items():
        main_local_change(connection, op="set", entity="employees", uid=uid, field=field,
                          value=value, actor=actor)
    connection.execute(
        text("UPDATE harness_sync_incomplete SET status = 'complete' WHERE entity = 'employees' AND uid = :u"),
        {"u": uid},
    )
    return {"uid": uid, "completed": sorted(values)}
