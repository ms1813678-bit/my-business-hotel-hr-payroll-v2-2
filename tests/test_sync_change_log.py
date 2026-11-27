"""Sync change log (0084): chain, counters, main_seq stamping, append-only database triggers, export shape.

The table is append-only at the database level, so these tests cannot delete their rows. Every test uses
its own random device id and only reads rows of that device, so the shared CI database stays consistent.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models import SyncChangeLog
from app.sync_change_log import (ChainBroken, SyncLogError, canonical_time, export_changes, record_change,
                                 row_hash, stamp_main_seq, value_of, verify_chain, verify_rows)
from app.sync_device_export import filter_rows

EMP = str(uuid.uuid4())
DEP = str(uuid.uuid4())
T0 = datetime(2026, 11, 27, 21, 30, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture()
def device():
    return "dev-" + uuid.uuid4().hex[:12]


def log(db, device, **kw):
    base = dict(device_id=device, entity="employees", uid=EMP, field="full_name", old="A", new="B", local_time=T0)
    base.update(kw)
    return record_change(db, **base)


def test_canonical_time_is_fixed_width_utc_and_refuses_naive():
    cairo = timezone(timedelta(hours=2))
    assert canonical_time(T0) == "2026-11-27T21:30:00.123456Z"
    assert canonical_time(datetime(2026, 11, 27, 23, 30, 0, 123456, tzinfo=cairo)) == "2026-11-27T21:30:00.123456Z"
    assert canonical_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "2026-01-02T03:04:05.000000Z"
    with pytest.raises(SyncLogError):
        canonical_time(datetime(2026, 11, 27, 21, 30))


def test_counters_chain_and_verify(device):
    with SessionLocal() as db:
        rows = [log(db, device, new=str(i)) for i in range(3)]
        assert [r.origin_counter for r in rows] == [1, 2, 3]
        assert rows[0].prev_hash == "0" * 64 and rows[1].prev_hash == rows[0].row_hash
        assert verify_chain(db, device) == 3
        db.rollback()
    with SessionLocal() as db:
        assert db.scalar(select(SyncChangeLog).where(SyncChangeLog.origin_device_id == device)) is None   # nothing committed by the module


def test_commit_belongs_to_the_caller_and_devices_have_separate_counters(device):
    other = device + "x"
    with SessionLocal.begin() as db:
        log(db, device)
        log(db, other)
        log(db, device)
    with SessionLocal() as db:
        assert verify_chain(db, device) == 2 and verify_chain(db, other) == 1


def test_tamper_gap_and_reorder_are_detected(device):
    with SessionLocal() as db:
        rows = [log(db, device, new=str(i)) for i in range(3)]
        assert verify_rows(rows) == 3
        with pytest.raises(ChainBroken):
            verify_rows([rows[0], rows[2]])               # a missing row
        with pytest.raises(ChainBroken):
            verify_rows([rows[1], rows[0], rows[2]])      # reordered
        rows[1].new_value = rows[1].new_value.replace("1", "9")
        with pytest.raises(ChainBroken):
            verify_rows(rows)                              # edited content
        db.rollback()


def test_hash_covers_every_field():
    base = ("p", "d", 1, "set", "employees", EMP, "full_name", "a", "b", "t", None)
    h = row_hash(*base)
    for i, other in enumerate(("q", "e", 2, "create", "departments", DEP, "position", "x", "y", "u", "ep")):
        changed = list(base)
        changed[i] = other
        assert row_hash(*changed) != h


@pytest.mark.parametrize("kw", [
    dict(entity="payroll_runs"), dict(uid="not-a-uid"), dict(uid=EMP.upper()), dict(field="basic_salary_typo"),
    dict(field="id"), dict(field="sync_uid"), dict(field="department_id"), dict(field="branch_id"),
    dict(field="branch_uid"), dict(field="branch_name"), dict(op="update"), dict(local_time=datetime(2026, 1, 1)),
    dict(new=object()), dict(device_id=""), dict(device_id="x" * 65), dict(field="*"),
])
def test_bad_input_is_refused(device, kw):
    with SessionLocal() as db, pytest.raises(SyncLogError):
        log(db, device, **kw)


def test_markers_and_parent_references(device):
    with SessionLocal() as db:
        c = log(db, device, op="create", field="*", old=None, new=None)
        d = log(db, device, op="delete", field="*", old=None, new=None)
        p = log(db, device, field="department_uid", old=None, new=DEP)
        assert (c.op, d.op, p.field) == ("create", "delete", "department_uid")
        assert value_of(p.new_value, "employees", EMP, "department_uid") == DEP
        with pytest.raises(SyncLogError):
            log(db, device, op="create", field="full_name", old=None, new="x")
        with pytest.raises(SyncLogError):
            log(db, device, op="delete", field="*", old=None, new="x")
        db.rollback()


def test_typed_values_round_trip(device):
    with SessionLocal() as db:
        s = log(db, device, field="basic_salary", old=Decimal("1000.50"), new=Decimal("1200.00"))
        h = log(db, device, field="hire_date", old=None, new=date(2026, 1, 31))
        assert value_of(s.new_value, "employees", EMP, "basic_salary") == Decimal("1200.00")
        assert value_of(h.new_value, "employees", EMP, "hire_date") == date(2026, 1, 31)
        assert h.old_value is None
        with pytest.raises(SyncLogError):
            value_of(s.new_value, "employees", EMP, "full_name")
        db.rollback()


def test_main_seq_is_stamped_once_in_order_and_exported_without_old_values(device):
    with SessionLocal.begin() as db:
        rows = [log(db, device, field="basic_salary", old=Decimal("1"), new=Decimal("2")),
                log(db, device, field="full_name", old="A", new="B")]
        top = stamp_main_seq(db, rows)
        assert [r.main_seq for r in rows] == [top - 1, top]
        assert stamp_main_seq(db, rows) == top and [r.main_seq for r in rows] == [top - 1, top]   # idempotent
    with SessionLocal() as db:
        out = [r for r in export_changes(db, top - 2) if r["origin_device_id"] == device]
        assert [r["field"] for r in out] == ["basic_salary", "full_name"]
        assert all("old_value" not in r and "old" not in r for r in out)
        assert export_changes(db, top) == []
        # the export feeds the role filter: the assistant never gets the salary row
        assert [r["field"] for r in filter_rows(out, "assistant_manager")] == ["full_name"]
        assert [r["field"] for r in filter_rows(out, "finance")] == ["basic_salary", "full_name"]
        with pytest.raises(SyncLogError):
            export_changes(db, -1)
        with pytest.raises(SyncLogError):
            export_changes(db, True)


def test_database_keeps_the_log_append_only(device):
    with SessionLocal.begin() as db:
        row = log(db, device)
        rid = row.id
    for sql in ("UPDATE sync_change_log SET new_value = 'x' WHERE id = :i",
                "UPDATE sync_change_log SET row_hash = repeat('a', 64) WHERE id = :i",
                "DELETE FROM sync_change_log WHERE id = :i"):
        with SessionLocal() as db, pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(sql), {"i": rid})
    with SessionLocal() as db, pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("TRUNCATE sync_change_log"))
    with SessionLocal.begin() as db:                       # the one allowed update: NULL -> number, once
        db.execute(text("UPDATE sync_change_log SET main_seq = (SELECT coalesce(max(main_seq), 0) + 1 FROM sync_change_log) WHERE id = :i"), {"i": rid})
    with SessionLocal() as db, pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE sync_change_log SET main_seq = main_seq + 1000 WHERE id = :i"), {"i": rid})


def test_database_rejects_a_duplicate_counter_and_a_marker_with_a_field(device):
    with SessionLocal.begin() as db:
        first = log(db, device)
        copy = dict(origin_device_id=device, origin_counter=first.origin_counter, op="set", entity="employees", uid=EMP,
                    field="full_name", local_time="t", prev_hash="0" * 64, row_hash="b" * 64)
    with SessionLocal() as db, pytest.raises(DBAPIError):
        db.add(SyncChangeLog(**copy))
        db.flush()
    with SessionLocal() as db, pytest.raises(DBAPIError):
        db.add(SyncChangeLog(**{**copy, "origin_counter": 99, "op": "delete", "row_hash": "c" * 64}))
        db.flush()
