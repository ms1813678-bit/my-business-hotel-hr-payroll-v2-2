"""Database helpers for the sync harness: engines, wire encoding, harness tables.

The business tables are the real ones from app.models (imported through
app.sync_uid.ENTITIES), so the scenarios run against real columns, real NOT NULL
constraints, real check constraints and the real sync_uid mapping. The
``harness_sync_*`` tables below are the harness's own stand-ins for the product
tables the design note describes (change log, per-device state, conflict list,
alerts, incomplete employees). They are deliberately namespaced so a later
product migration cannot collide with them; when the real applier is plugged in,
the harness stops creating them.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import Numeric, Boolean, Date, DateTime, Integer, String, Text, text
from sqlalchemy.engine import Connection, Engine

from app import sync_visibility
from app.sync_uid import ENTITIES as SYNC_ENTITIES
from app.sync_uid import PARENT_REFS

BUSINESS_ENTITIES = ("departments", "employees", "leave_types", "leave_records")

# Columns that never travel: the numeric id is device-local, branch_id has no
# UID foundation (app.sync_uid.OUT_OF_SCOPE_REFS) and photo_path is a local path
# (app.sync_visibility.DENIED_ALL).
NEVER_SYNCED = {"id", "branch_id", "photo_path"}

HARNESS_TABLES_SQL = (
    """CREATE TABLE IF NOT EXISTS harness_sync_meta (
        key text PRIMARY KEY,
        value text NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_device (
        device_id text PRIMARY KEY,
        role text NOT NULL,
        last_seq bigint NOT NULL DEFAULT 0,
        last_hash text NOT NULL DEFAULT '',
        last_confirmed_counter bigint NOT NULL DEFAULT 0,
        epoch_seen text,
        paired_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_change (
        id bigserial PRIMARY KEY,
        device_id text NOT NULL,
        seq bigint NOT NULL,
        op text NOT NULL,
        entity text NOT NULL,
        uid text NOT NULL,
        field text NOT NULL DEFAULT '',
        old_value text,
        new_value text,
        local_time timestamptz,
        received_at timestamptz NOT NULL DEFAULT now(),
        counter bigint NOT NULL,
        status text NOT NULL,
        session_id text,
        prev_hash text NOT NULL DEFAULT '',
        hash text NOT NULL DEFAULT '',
        UNIQUE (device_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_outbox (
        seq bigserial PRIMARY KEY,
        op text NOT NULL,
        entity text NOT NULL,
        uid text NOT NULL,
        field text NOT NULL DEFAULT '',
        old_value text,
        new_value text,
        local_time timestamptz NOT NULL,
        prev_hash text NOT NULL DEFAULT '',
        hash text NOT NULL DEFAULT '',
        state text NOT NULL DEFAULT 'queued',
        session_id text,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_conflict (
        id bigserial PRIMARY KEY,
        kind text NOT NULL,
        entity text NOT NULL,
        uid text NOT NULL,
        field text NOT NULL DEFAULT '',
        device_id text NOT NULL,
        device_seq bigint,
        device_value text,
        device_time timestamptz,
        main_value text,
        main_time timestamptz,
        main_device text,
        status text NOT NULL DEFAULT 'pending',
        resolution text,
        resolved_by text,
        resolved_at timestamptz,
        notified_at timestamptz,
        session_id text,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_alert (
        id bigserial PRIMARY KEY,
        kind text NOT NULL,
        device_id text,
        entity text,
        uid text,
        field text,
        detail text,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_incomplete (
        entity text NOT NULL,
        uid text NOT NULL,
        device_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        status text NOT NULL DEFAULT 'pending',
        PRIMARY KEY (entity, uid)
    )""",
    """CREATE TABLE IF NOT EXISTS harness_sync_session (
        session_id text PRIMARY KEY,
        device_id text NOT NULL,
        request_sha256 text NOT NULL,
        response_json text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        confirmed_at timestamptz
    )""",
)


def make_engine(url: str) -> Engine:
    """One engine per instance database. psycopg3, small pool, pre-ping."""
    from sqlalchemy import create_engine

    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=4, future=True)


def ensure_harness_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        for statement in HARNESS_TABLES_SQL:
            connection.execute(text(statement))


# ---------------------------------------------------------------- values


def encode_value(value: Any) -> Any:
    """DB value -> JSON-native wire value. Decimals keep every digit as text."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        return format(value, "f")
    raise TypeError(f"cannot put {type(value).__name__} on the wire")


def decode_value(value: Any, column) -> Any:
    """Wire value -> the python type the target column expects."""
    if value is None:
        return None
    column_type = column.type
    if isinstance(column_type, Boolean):
        if not isinstance(value, bool):
            raise ValueError(f"{column.name}: expected a boolean")
        return value
    if isinstance(column_type, Numeric):
        return decimal.Decimal(str(value))
    if isinstance(column_type, DateTime):
        if isinstance(value, dt.datetime):
            return value
        return dt.datetime.fromisoformat(str(value))
    if isinstance(column_type, Date):
        return dt.date.fromisoformat(str(value)[:10])
    if isinstance(column_type, Integer):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{column.name}: expected an integer")
        return value
    if isinstance(column_type, (String, Text)):
        if not isinstance(value, str):
            raise ValueError(f"{column.name}: expected text")
        return value
    raise TypeError(f"{column.name}: unsupported column type {column_type!r}")


def entity_table(entity: str):
    if entity not in SYNC_ENTITIES:
        raise KeyError(f"entity outside the sync scope: {entity!r}")
    return SYNC_ENTITIES[entity].__table__


def entity_columns(entity: str) -> list[str]:
    return [c.name for c in entity_table(entity).c if c.name not in NEVER_SYNCED]


def wire_key(entity: str, column: str) -> str:
    """employees.department_id travels as department_uid."""
    parent = PARENT_REFS.get(entity, {}).get(column)
    return f"{column[:-3]}_uid" if parent else column


# ---------------------------------------------------------------- rows


def row_for_uid(connection: Connection, entity: str, uid: str):
    table = entity_table(entity)
    return connection.execute(table.select().where(table.c.sync_uid == uid)).mappings().first()


def row_payload(connection: Connection, entity: str, row, role: str) -> dict:
    """A row as one role's device may see it, in wire form (parent refs as uids).

    Uses app.sync_visibility.project for the column policy, so the harness and
    the product cannot drift on who sees what.
    """
    table = entity_table(entity)
    values = {}
    for column in table.c:
        if column.name in NEVER_SYNCED:
            continue
        values[column.name] = encode_value(row[column.name])
    payload = sync_visibility.project(entity, role, values)
    out = {}
    for column, value in payload.items():
        out[wire_key(entity, column)] = value
    if entity in PARENT_REFS:
        for column, parent_entity in PARENT_REFS[entity].items():
            if column in payload and payload[column] is not None:
                parent_table = entity_table(parent_entity)
                parent_uid = connection.execute(
                    parent_table.select().where(parent_table.c.id == payload[column])
                ).mappings().first()
                parent_uid = None if parent_uid is None else parent_uid["sync_uid"]
                out[wire_key(entity, column)] = parent_uid
    if (entity, "branch_name") in sync_visibility.DERIVED_COLUMNS and \
            "branch_name" in sync_visibility.visible_columns(entity, role):
        out["branch_name"] = _branch_name(connection, row)
    return out


def _branch_name(connection: Connection, row) -> str | None:
    branch_id = row.get("branch_id")
    if branch_id is None:
        return None
    return connection.execute(
        text("SELECT name FROM branches WHERE id = :id"), {"id": branch_id}
    ).scalar()


def record_sha256(connection: Connection, entity: str, uid: str, role: str) -> str | None:
    """Hash of one record as one role sees it: the base a delete is compared on."""
    row = row_for_uid(connection, entity, uid)
    if row is None:
        return None
    payload = row_payload(connection, entity, row, role)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _orm_session(connection: Connection):
    """app.sync_uid maps rows through the ORM, so give it a Session on this transaction."""
    from sqlalchemy.orm import Session

    return Session(bind=connection, autoflush=False, join_transaction_mode="create_savepoint")


def insert_row(connection: Connection, entity: str, uid: str, payload: dict, *, role: str,
               placeholders: dict | None = None) -> None:
    """Insert one row from a wire payload, filling hidden NOT NULL columns.

    A role that cannot see a NOT NULL column cannot supply it; the record is
    then created with a placeholder and recorded as incomplete (design note rule
    8). ``payload`` keys are wire keys.
    """
    table = entity_table(entity)
    from app.sync_uid import uids_to_refs

    with _orm_session(connection) as session:
        db_values = uids_to_refs(session, entity, dict(payload))
    row = {"sync_uid": uid}
    for name, value in db_values.items():
        if name in table.c:
            row[name] = decode_value(value, table.c[name])
    for column in table.c:
        if column.name in row or column.name in ("id", "sync_uid"):
            continue
        if column.nullable or column.default is not None or column.server_default is not None:
            continue
        row[column.name] = (placeholders or {}).get(column.name, placeholder_for(column))
    connection.execute(table.insert().values(**row))


def placeholder_for(column) -> Any:
    """A value that satisfies the column without pretending to be real data."""
    column_type = column.type
    if isinstance(column_type, Boolean):
        return False
    if isinstance(column_type, Numeric):
        return decimal.Decimal(0)
    if isinstance(column_type, Integer):
        return 0
    if isinstance(column_type, DateTime):
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(column_type, Date):
        return dt.date(2000, 1, 1)
    return ""


def update_field(connection: Connection, entity: str, uid: str, field: str, value: Any) -> None:
    """Write one wire field to a row (parent uids resolved through app.sync_uid)."""
    table = entity_table(entity)
    from app.sync_uid import uids_to_refs

    if field.endswith("_uid"):
        column = field[:-4] + "_id"
        if column not in table.c:
            raise KeyError(f"{entity} has no column for {field}")
        with _orm_session(connection) as session:
            resolved = uids_to_refs(session, entity, {field: value})[column]
        connection.execute(table.update().where(table.c.sync_uid == uid).values({column: resolved}))
        return
    if field not in table.c:
        raise KeyError(f"{entity} has no column {field}")
    connection.execute(
        table.update().where(table.c.sync_uid == uid).values({field: decode_value(value, table.c[field])})
    )


def delete_row(connection: Connection, entity: str, uid: str) -> None:
    table = entity_table(entity)
    connection.execute(table.delete().where(table.c.sync_uid == uid))


def next_employee_code(connection: Connection, existing: str) -> str:
    """Next sequential employee code in the shape the duplicate was typed in.

    Rule 7: the main device gives the next sequential code. The shape (prefix,
    digits, width) is taken from the code that collided.
    """
    import re

    match = re.fullmatch(r"(?P<prefix>.*?)(?P<digits>\d+)(?P<suffix>\D*)", existing or "")
    if not match:
        raise ValueError(f"cannot derive a sequential code from {existing!r}")
    prefix, digits, suffix = match.group("prefix"), match.group("digits"), match.group("suffix")
    width = len(digits)
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)" + re.escape(suffix) + r"$")
    numbers = []
    for (code,) in connection.execute(text("SELECT code FROM employees")):
        found = pattern.match(code or "")
        if found:
            numbers.append(int(found.group(1)))
    return f"{prefix}{max(numbers + [0]) + 1:0{width}d}{suffix}"


def code_in_use(connection: Connection, code: str) -> bool:
    return connection.execute(
        text("SELECT 1 FROM employees WHERE code = :code"), {"code": code}
    ).first() is not None


def meta_get(connection: Connection, key: str) -> str | None:
    return connection.execute(
        text("SELECT value FROM harness_sync_meta WHERE key = :key"), {"key": key}
    ).scalar()


def meta_set(connection: Connection, key: str, value: str) -> None:
    connection.execute(
        text("INSERT INTO harness_sync_meta(key, value) VALUES (:key, :value) "
             "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
        {"key": key, "value": value},
    )


def next_counter(connection: Connection) -> int:
    """Main's broadcast counter: every applied change gets the next value."""
    current = int(meta_get(connection, "counter") or 1)
    meta_set(connection, "counter", str(current + 1))
    return current + 1


def current_counter(connection: Connection) -> int:
    return int(meta_get(connection, "counter") or 1)


def rows(connection: Connection, statement) -> Iterable:
    return connection.execute(statement).mappings().all()
