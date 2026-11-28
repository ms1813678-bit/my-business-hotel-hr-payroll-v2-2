import uuid
from datetime import datetime, timedelta, timezone
import pytest
from app.sync_apply import RETURNED, SessionPlan
from app.sync_apply_ops import digest, is_expired, plan_to_ops, returned_record
from app.sync_change_log import SyncLogError
from tests.test_sync_apply import DEV, EMP, mk, plan

T0 = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)


def test_ready_plan_gives_ops_in_counter_order_and_last_counter():
    rows = mk([("create", "employees", EMP, "*", None, None), ("set", "employees", EMP, "full_name", None, "B")])
    p = plan(list(reversed(rows)))
    s = plan_to_ops(p, device_id=DEV)
    assert [o.origin_counter for o in s.ops] == [1, 2] and [o.kind for o in s.ops] == ["create", "set"]
    assert s.last_counter == 2 and not s.conflicts and s.duplicates == 0


def test_conflicts_and_already_deleted_count_as_consumed_and_keep_values():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("delete", "employees", EMP, "*", None, None)])
    p = plan(rows, values={("employees", EMP, "full_name"): "Z"}, deleted={("employees", EMP)})
    s = plan_to_ops(p, device_id=DEV)
    assert s.ops == () and s.last_counter == 2
    assert [c["reason"] for c in s.conflicts] == ["edit_on_deleted"] and s.conflicts[0]["new_value"] == "B"
    assert [o.origin_counter for o in s.already_deleted] == [2]


def test_retry_after_apply_is_all_duplicates_no_ops():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    first = plan_to_ops(plan(rows, values={("employees", EMP, "full_name"): "A"}), device_id=DEV)
    applied = {rows[0]["origin_counter"]: rows[0]["row_hash"]}
    again = plan_to_ops(plan(rows, applied=applied, last=1), device_id=DEV, applied_last_counter=1)
    assert first.ops and again.ops == () and again.duplicates == 1 and again.last_counter == 1
    assert first.session_digest == again.session_digest


def test_returned_plan_cannot_become_ops_and_record_has_no_values():
    rows = mk([("set", "employees", EMP, "basic_salary", "1000", "2000")])
    p = plan(rows, role="assistant_manager")
    assert p.status == RETURNED
    with pytest.raises(SyncLogError):
        plan_to_ops(p, device_id=DEV)
    rec = returned_record(p, device_id=DEV, rows=rows, received_at=T0)
    assert rec["warning"]["field"] == "basic_salary" and rec["row_count"] == 1
    assert "1000" not in repr(rec) and "2000" not in repr(rec)
    assert rec["received_at"] == "2026-11-28T09:00:00.000000Z"


def test_returned_record_refuses_ready_plan_and_naive_time():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    with pytest.raises(SyncLogError):
        returned_record(plan(rows, values={("employees", EMP, "full_name"): "A"}), device_id=DEV, rows=rows, received_at=T0)
    bad = plan(mk([("set", "employees", EMP, "basic_salary", "1", "2")]), role="assistant_manager")
    with pytest.raises(SyncLogError):
        returned_record(bad, device_id=DEV, rows=rows, received_at=datetime(2026, 1, 1))


def test_expiry_needs_explicit_days_and_is_strict():
    rows = mk([("set", "employees", EMP, "basic_salary", "1", "2")])
    rec = returned_record(plan(rows, role="assistant_manager"), device_id=DEV, rows=rows, received_at=T0)
    assert not is_expired(rec, T0 + timedelta(days=30), 30) and is_expired(rec, T0 + timedelta(days=30, seconds=1), 30)
    for d in (0, -1, True, None, "30"):
        with pytest.raises(SyncLogError):
            is_expired(rec, T0, d)
    with pytest.raises(SyncLogError):
        is_expired(rec, datetime(2026, 1, 1), 30)


def test_digest_is_order_independent():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "nationality", None, "X")])
    assert digest(rows) == digest(list(reversed(rows)))


def test_foreign_device_row_refused():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    p = plan(rows, values={("employees", EMP, "full_name"): "A"})
    with pytest.raises(SyncLogError):
        plan_to_ops(p, device_id="other-device")


def test_returned_record_row_count_with_a_generator():
    rows = mk([("set", "employees", EMP, "basic_salary", "1", "2"), ("set", "employees", EMP, "full_name", "A", "B")])
    p = plan(rows, role="assistant_manager")
    rec = returned_record(p, device_id=DEV, rows=(r for r in rows), received_at=T0)
    assert rec["row_count"] == 2 and rec["session_digest"] == digest(rows)


def test_received_at_is_stamped_in_canonical_utc_and_optional():
    from datetime import timedelta as td
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    p = plan(rows, values={("employees", EMP, "full_name"): "A"})
    assert plan_to_ops(p, device_id=DEV).received_at is None
    cairo = timezone(td(hours=2))
    s = plan_to_ops(p, device_id=DEV, received_at=datetime(2026, 11, 28, 11, 0, 5, 123, tzinfo=cairo))
    assert s.received_at == "2026-11-28T09:00:05.000123Z"
    with pytest.raises(SyncLogError):
        plan_to_ops(p, device_id=DEV, received_at=datetime(2026, 11, 28, 9, 0))
