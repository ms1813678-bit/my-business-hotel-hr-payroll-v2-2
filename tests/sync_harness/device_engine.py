"""Device side of the reference sync engine (test double, not product code).

A device works offline: its own edits apply to its own database immediately and
are queued in an append-only outbox with a per-device counter and a hash chain.
A sync session sends the queue, applies what the main device sends back in one
transaction, and confirms. The device never talks to another device.

Local state lives in the same ``harness_sync_*`` tables as the main side, in the
device's own database: ``harness_sync_outbox`` is the queue, and
``harness_sync_meta`` holds the device id, its role, the last main epoch it saw
and the last main counter it applied.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app import sync_visibility
from app.sync_uid import ENTITIES as SYNC_ENTITIES
from app.sync_uid import canonical_uid

from . import contract, store


class DeviceError(RuntimeError):
    pass


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"))


def _unjson(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def initialise(connection: Connection, *, device_id: str, role: str, epoch: str | None) -> None:
    store.meta_set(connection, "device_id", device_id)
    store.meta_set(connection, "role", role)
    store.meta_set(connection, "epoch_seen", epoch or "")
    store.meta_set(connection, "applied_counter", "0")


def identity(connection: Connection) -> dict:
    device_id = store.meta_get(connection, "device_id")
    role = store.meta_get(connection, "role")
    if not device_id or not role:
        raise DeviceError("device is not initialised")
    epoch = store.meta_get(connection, "epoch_seen") or None
    return {"device_id": device_id, "role": role, "epoch": epoch,
            "applied_counter": int(store.meta_get(connection, "applied_counter") or 0)}


def _last_outbox(connection: Connection):
    return connection.execute(
        text("SELECT seq, hash FROM harness_sync_outbox ORDER BY seq DESC LIMIT 1")
    ).mappings().first()


def queue_local_change(connection: Connection, *, op: str, entity: str, uid: str,
                       field: str = "", value: Any = None,
                       local_time: dt.datetime | None = None) -> dict:
    """A screen edit on this device: apply locally now, queue for the next sync."""
    me = identity(connection)
    if entity not in SYNC_ENTITIES:
        raise DeviceError(f"entity outside sync scope: {entity!r}")
    canonical_uid(uid)
    role = me["role"]
    if op == "create":
        if store.row_for_uid(connection, entity, uid) is not None:
            raise DeviceError("record already exists on this device")
        payload = dict(value or {})
        store.insert_row(connection, entity, uid, payload, role=role)
        old = None
        new = payload
        field = ""
    elif op == "set":
        row = store.row_for_uid(connection, entity, uid)
        if row is None:
            raise DeviceError("record does not exist on this device")
        base_column = field[:-4] + "_id" if field.endswith("_uid") else field
        if base_column not in sync_visibility.visible_columns(entity, role):
            raise DeviceError(f"this device may not edit {entity}.{field}")
        old = store.row_payload(connection, entity, row, role).get(field)
        store.update_field(connection, entity, uid, field, value)
        new = value
    elif op == "delete":
        row = store.row_for_uid(connection, entity, uid)
        if row is None:
            raise DeviceError("record does not exist on this device")
        old = store.record_sha256(connection, entity, uid, role)
        store.delete_row(connection, entity, uid)
        new = None
    else:
        raise DeviceError(f"unknown op {op!r}")

    last = _last_outbox(connection)
    prev_hash = (last["hash"] if last else "") or ""
    seq = int(last["seq"]) + 1 if last else 1
    when = local_time or dt.datetime.now(dt.timezone.utc)
    change = contract.make_change(
        prev_hash, device_id=me["device_id"], seq=seq, op=op, entity=entity, uid=uid,
        field=field, old=old, new=new, local_time=when.astimezone(dt.timezone.utc).isoformat(),
    )
    connection.execute(
        text("""INSERT INTO harness_sync_outbox(seq, op, entity, uid, field, old_value, new_value,
                 local_time, prev_hash, hash) VALUES (:seq, :op, :entity, :uid, :field, :old,
                 :new, :local_time, :prev_hash, :hash)"""),
        {"seq": seq, "op": op, "entity": entity, "uid": uid, "field": field, "old": _json(old),
         "new": _json(new), "local_time": when, "prev_hash": prev_hash, "hash": change["hash"]},
    )
    return {"seq": seq, "hash": change["hash"]}


def build_request(connection: Connection, *, session_id: str) -> dict:
    """The session the device sends: its whole unsent queue plus its base counter."""
    me = identity(connection)
    rows = connection.execute(
        text("""SELECT seq, op, entity, uid, field, old_value, new_value, local_time, prev_hash, hash
                FROM harness_sync_outbox WHERE state = 'queued' ORDER BY seq""")
    ).mappings().all()
    changes = []
    for row in rows:
        changes.append({
            "device_id": me["device_id"], "seq": int(row["seq"]), "op": row["op"],
            "entity": row["entity"], "uid": row["uid"], "field": row["field"],
            "old": _unjson(row["old_value"]), "new": _unjson(row["new_value"]),
            "local_time": row["local_time"].astimezone(dt.timezone.utc).isoformat(), "prev_hash": row["prev_hash"],
            "hash": row["hash"],
        })
    return {"protocol": contract.PROTOCOL, "session_id": session_id,
            "device_id": me["device_id"], "role": me["role"], "epoch": me["epoch"],
            "base_counter": me["applied_counter"], "changes": changes}


def _has_queued(connection: Connection, entity: str, uid: str, field: str) -> bool:
    return connection.execute(
        text("""SELECT 1 FROM harness_sync_outbox
                WHERE state = 'queued' AND entity = :e AND uid = :u AND field = :f LIMIT 1"""),
        {"e": entity, "u": uid, "f": field},
    ).first() is not None


def apply_response(connection: Connection, response: dict, *, role: str) -> None:
    """Record what the main device decided about this device's changes."""
    for seq in response.get("applied", []) + response.get("duplicate", []):
        connection.execute(text("UPDATE harness_sync_outbox SET state = 'applied' WHERE seq = :s"),
                           {"s": seq})
    for conflict in response.get("conflicts", []):
        connection.execute(text("UPDATE harness_sync_outbox SET state = 'conflict' WHERE seq = :s"),
                           {"s": conflict["seq"]})
    for rejected in response.get("rejected", []):
        connection.execute(text("UPDATE harness_sync_outbox SET state = 'rejected' WHERE seq = :s"),
                           {"s": rejected["seq"]})
    for correction in response.get("code_corrections", []):
        store.update_field(connection, correction["entity"], correction["uid"], "code",
                           correction["new"])
    for resolved in response.get("resolved", []):
        if resolved["outcome"] == "accepted":
            connection.execute(text("UPDATE harness_sync_outbox SET state = 'applied' WHERE seq = :s"),
                               {"s": resolved["seq"]})
            if resolved.get("kind") == "delete_vs_edit":
                store.delete_row(connection, resolved["entity"], resolved["uid"])
        else:
            connection.execute(text("UPDATE harness_sync_outbox SET state = 'rejected' WHERE seq = :s"),
                               {"s": resolved["seq"]})
            if resolved.get("kind") == "delete_vs_edit" and isinstance(resolved.get("main_value"), dict):
                # The delete was refused: the record the device removed locally has to
                # come back, with the main device's values.
                if store.row_for_uid(connection, resolved["entity"], resolved["uid"]) is None:
                    store.insert_row(connection, resolved["entity"], resolved["uid"],
                                     resolved["main_value"], role=role)
            elif resolved.get("field") and resolved.get("main_value") is not None:
                store.update_field(connection, resolved["entity"], resolved["uid"],
                                   resolved["field"], resolved["main_value"])
    if response.get("status") == "ok":
        store.meta_set(connection, "epoch_seen", response.get("main_epoch") or "")


def apply_pull(connection: Connection, items: list[dict], *, role: str,
               fault: Callable[[str], None] | None = None) -> int:
    """Apply the main device's changes in one transaction. Returns the new counter."""
    applied = int(store.meta_get(connection, "applied_counter") or 0)
    total = len(items)
    for index, item in enumerate(items):
        entity = item["entity"]
        if entity not in SYNC_ENTITIES:
            raise DeviceError(f"main device sent an unknown entity {entity!r}")
        op = item["op"]
        if op == "set":
            field = item["field"]
            if (field[:-4] + "_id" if field.endswith("_uid") else field) not in \
                    sync_visibility.visible_columns(entity, role):
                raise DeviceError(f"main device sent a field this role may not see: {entity}.{field}")
            if not _has_queued(connection, entity, item["uid"], field):
                if store.row_for_uid(connection, entity, item["uid"]) is None:
                    continue
                store.update_field(connection, entity, item["uid"], field, item["new"])
        elif op == "create":
            payload = dict(item["new"])
            for key in payload:
                column = key[:-4] + "_id" if key.endswith("_uid") else key
                if key != "branch_name" and column not in sync_visibility.visible_columns(entity, role):
                    raise DeviceError(f"main device sent a hidden field {entity}.{key}")
            row = store.row_for_uid(connection, entity, item["uid"])
            if row is None:
                store.insert_row(connection, entity, item["uid"], payload, role=role)
            else:
                for key, value in payload.items():
                    column = key[:-4] + "_id" if key.endswith("_uid") else key
                    if key == "branch_name" or column not in store.entity_table(entity).c:
                        continue
                    if _has_queued(connection, entity, item["uid"],
                                   key if key.endswith("_uid") else column):
                        continue
                    store.update_field(connection, entity, item["uid"],
                                       key if key.endswith("_uid") else column, value)
        elif op == "delete":
            store.delete_row(connection, entity, item["uid"])
            connection.execute(
                text("""UPDATE harness_sync_outbox SET state = 'superseded'
                        WHERE state = 'queued' AND entity = :e AND uid = :u"""),
                {"e": entity, "u": item["uid"]},
            )
        applied = max(applied, int(item["counter"]))
        if fault is not None and total > 1 and index == total // 2:
            fault("device_mid_apply")
    store.meta_set(connection, "applied_counter", str(applied))
    return applied


def rebaseline_local(connection: Connection, *, main_epoch: str | None) -> dict:
    """Accept the main device as the truth after a restore.

    The device's unsent edits are kept as local conflicts (shown to its user and
    re-submittable after the administrator decides); they are not re-sent, so a
    restored main device is never overwritten by an older device's queue.
    """
    queued = connection.execute(
        text("SELECT seq, entity, uid, field, new_value FROM harness_sync_outbox "
             "WHERE state = 'queued' ORDER BY seq")
    ).mappings().all()
    for entry in queued:
        connection.execute(
            text("""INSERT INTO harness_sync_conflict(kind, entity, uid, field, device_id,
                     device_seq, device_value, status)
                    VALUES ('restore_rebaseline', :e, :u, :f, :d, :seq, :value, 'pending')"""),
            {"e": entry["entity"], "u": entry["uid"], "f": entry["field"],
             "d": store.meta_get(connection, "device_id"), "seq": entry["seq"],
             "value": entry["new_value"]},
        )
    connection.execute(
        text("UPDATE harness_sync_outbox SET state = 'conflict' WHERE state = 'queued'")
    )
    store.meta_set(connection, "epoch_seen", main_epoch or "")
    store.meta_set(connection, "applied_counter", "0")
    return {"kept_as_conflicts": len(queued), "epoch": main_epoch}


def pending_state(connection: Connection) -> dict:
    me = identity(connection)
    queued = connection.execute(
        text("SELECT count(*) FROM harness_sync_outbox WHERE state = 'queued'")
    ).scalar()
    return dict(me, queued=queued)
