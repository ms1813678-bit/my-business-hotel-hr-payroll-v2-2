"""Cross-database checks: is every device's view equal to the main device's?

Rule 5 means the three databases are not byte-identical: an assistant device
never holds salary, national id or phone, and a finance device never holds the
assistant's derived branch name. So equality is checked per role: the main
device's projection for that role must equal the device's own projection, and the
row set (which uids exist) must match everywhere. That is the data-loss check the
design note asks for, stated in the only form rule 5 allows.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import sync_visibility

from . import store


def projection(engine: Engine, role: str) -> dict[tuple[str, str], dict]:
    """{(entity, uid): {wire field: value}} for everything this role may see."""
    out: dict[tuple[str, str], dict] = {}
    with engine.connect() as connection:
        for entity in store.BUSINESS_ENTITIES:
            table = store.entity_table(entity)
            for row in connection.execute(table.select()).mappings().all():
                out[(entity, row["sync_uid"])] = store.row_payload(connection, entity, row, role)
    return out


def content_hash(engine: Engine, role: str) -> str:
    payload = projection(engine, role)
    text_form = json.dumps({f"{entity}/{uid}": values for (entity, uid), values in payload.items()},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text_form.encode("utf-8")).hexdigest()


def row_counts(engine: Engine) -> dict[str, int]:
    counts = {}
    with engine.connect() as connection:
        for entity in store.BUSINESS_ENTITIES:
            counts[entity] = connection.execute(
                text(f"SELECT count(*) FROM {store.entity_table(entity).name}")).scalar()
    return counts


def differences(left: Engine, right: Engine, role: str) -> list[str]:
    """Human-readable differences between two databases as one role sees them."""
    left_projection = projection(left, role)
    right_projection = projection(right, role)
    problems: list[str] = []
    for key in sorted(set(left_projection) - set(right_projection)):
        problems.append(f"only on the left: {key[0]} {key[1]}")
    for key in sorted(set(right_projection) - set(left_projection)):
        problems.append(f"only on the right: {key[0]} {key[1]}")
    for key in sorted(set(left_projection) & set(right_projection)):
        left_values = left_projection[key]
        right_values = right_projection[key]
        # branch_name is derived on the main device and delivered read-only; a device
        # has no branch table to hold it in (finding), so it is not compared here.
        for field in sorted((set(left_values) | set(right_values)) - {"branch_name"}):
            if left_values.get(field) != right_values.get(field):
                problems.append(
                    f"{key[0]} {key[1]}.{field}: {left_values.get(field)!r} != {right_values.get(field)!r}")
    return problems


def assert_equal(left: Engine, right: Engine, role: str, *, label: str) -> None:
    problems = differences(left, right, role)
    if problems:
        raise AssertionError(f"{label}: {len(problems)} difference(s)\n  " + "\n  ".join(problems[:20]))


def snapshot(engine: Engine, role: str) -> dict:
    """Everything needed to prove a crashed session left no partial rows."""
    with engine.connect() as connection:
        return {
            "rows": projection(engine, role),
            "counts": row_counts(engine),
            "counter": store.current_counter(connection),
            "changes": connection.execute(
                text("SELECT count(*) FROM harness_sync_change")).scalar(),
        }


def assert_unchanged(before: dict, engine: Engine, role: str, *, label: str) -> None:
    after = snapshot(engine, role)
    if before["rows"] != after["rows"]:
        changed = [f"{k[0]}/{k[1]}" for k in set(before["rows"]) | set(after["rows"])
                   if before["rows"].get(k) != after["rows"].get(k)]
        raise AssertionError(f"{label}: data changed in a session that must have rolled back: {changed[:10]}")
    if before["counter"] != after["counter"]:
        raise AssertionError(f"{label}: counter moved {before['counter']} -> {after['counter']}")
    if before["changes"] != after["changes"]:
        raise AssertionError(f"{label}: change log grew {before['changes']} -> {after['changes']}")


def pending_conflicts(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(
            text("SELECT id, kind, entity, uid, field, device_id, device_value, main_value, status "
                 "FROM harness_sync_conflict WHERE status = 'pending' ORDER BY id")).mappings()]


def conflict_count(engine: Engine, status: str | None = None) -> int:
    with engine.connect() as connection:
        if status is None:
            return connection.execute(text("SELECT count(*) FROM harness_sync_conflict")).scalar()
        return connection.execute(text("SELECT count(*) FROM harness_sync_conflict WHERE status = :s"),
                                  {"s": status}).scalar()


def alerts(engine: Engine, kind: str | None = None) -> list[dict]:
    with engine.connect() as connection:
        if kind is None:
            rows = connection.execute(text("SELECT * FROM harness_sync_alert ORDER BY id")).mappings()
        else:
            rows = connection.execute(text("SELECT * FROM harness_sync_alert WHERE kind = :k ORDER BY id"),
                                      {"k": kind}).mappings()
        return [dict(row) for row in rows]


def role_for_device(name: str) -> str:
    return {"A": sync_visibility.ROLE_ADMIN, "B": sync_visibility.ROLE_ASSISTANT,
            "C": sync_visibility.ROLE_FINANCE}[name]
