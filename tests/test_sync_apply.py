"""sync_apply.plan_session: pure decisions, no database."""
import uuid

import pytest

from app.sync_apply import READY, RETURNED, plan_session
from app.sync_change_log import GENESIS_HASH, ChainBroken, SyncLogError, row_hash

DEV = "dev-test-1"
EMP = str(uuid.uuid4())
def ALLOW(role, entity, field):
    return True


def mk(rows_spec, device=DEV, start=1, prev=GENESIS_HASH):
    out = []
    for i, (op, entity, uid, field, old, new) in enumerate(rows_spec):
        c = start + i
        h = row_hash(prev, device, c, op, entity, uid, field, old, new, "2026-11-27T21:30:00.000000Z", None)
        out.append({"origin_device_id": device, "origin_counter": c, "op": op, "entity": entity, "uid": uid,
                    "field": field, "old_value": old, "new_value": new, "local_time": "2026-11-27T21:30:00.000000Z",
                    "restore_epoch": None, "prev_hash": prev, "row_hash": h})
        prev = h
    return out


def plan(rows, role="system_admin", applied=None, last=0, values=None, deleted=(), touched=(), may_edit=ALLOW):
    return plan_session(role=role, device_id=DEV, rows=rows, applied=applied or {}, last_counter=last,
                        main_values=values or {}, main_deleted=set(deleted), main_touched=set(touched), may_edit=may_edit)


def test_clean_session_applies_and_chained_edits_are_not_a_conflict():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "full_name", "B", "C")])
    p = plan(rows, values={("employees", EMP, "full_name"): "A"})
    assert p.status == READY and len(p.apply) == 2 and not p.conflicts and p.warning is None


def test_different_fields_and_records_merge():
    other = str(uuid.uuid4())
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", other, "nationality", None, "X")])
    p = plan(rows, values={("employees", EMP, "full_name"): "A"})
    assert len(p.apply) == 2 and not p.conflicts


def test_same_field_changed_on_main_is_a_conflict_and_the_rest_continues():
    other = str(uuid.uuid4())
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", other, "nationality", None, "X")])
    p = plan(rows, values={("employees", EMP, "full_name"): "Z"})
    assert p.status == READY and [c[1] for c in p.conflicts] == ["same_field_changed"] and len(p.apply) == 1


def test_hidden_field_returns_the_whole_session_with_a_warning_and_applies_nothing():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "basic_salary", "1", "2")])
    p = plan(rows, role="assistant_manager", values={("employees", EMP, "full_name"): "A"})
    assert p.status == RETURNED and not p.apply and not p.conflicts
    assert p.warning == {"reason": "hidden_field", "entity": "employees", "uid": EMP, "field": "basic_salary",
                         "origin_device_id": DEV, "origin_counter": 2}
    assert set(p.warning) == {"reason", "entity", "uid", "field", "origin_device_id", "origin_counter"}  # no values


def test_no_edit_right_returns_the_session_even_for_a_visible_field():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    p = plan(rows, may_edit=lambda *a: False)
    assert p.status == RETURNED and p.warning["reason"] == "not_editable" and not p.apply


def test_an_edit_policy_is_required():
    with pytest.raises(SyncLogError):
        plan(mk([("set", "employees", EMP, "full_name", "A", "B")]), may_edit=None)


def test_duplicates_are_no_ops_and_new_rows_continue():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "nationality", None, "X")])
    p = plan(rows, applied={1: rows[0]["row_hash"]}, last=1, values={("employees", EMP, "nationality"): None})
    assert len(p.duplicates) == 1 and len(p.apply) == 1


def test_gap_reuse_and_edited_row_are_integrity_failures():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "full_name", "B", "C")])
    with pytest.raises(ChainBroken):
        plan(rows[1:])  # starts at counter 2 with nothing applied
    with pytest.raises(ChainBroken):
        plan([{**rows[0], "new_value": "Q"}])  # content no longer matches its hash
    with pytest.raises(ChainBroken):
        plan(rows[:1], applied={1: "f" * 64}, last=1)  # counter 1 already used with other content


def test_deletes_and_edits_go_to_conflicts_not_silently_applied():
    rows = mk([("delete", "employees", EMP, "*", None, None)])
    assert plan(rows, touched={("employees", EMP)}).conflicts[0][1] == "delete_against_edit"
    assert len(plan(rows).apply) == 1
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    assert plan(rows, deleted={("employees", EMP)}).conflicts[0][1] == "edit_on_deleted"
    rows = mk([("delete", "employees", EMP, "*", None, None), ("set", "employees", EMP, "full_name", "A", "B")])
    p = plan(rows, values={("employees", EMP, "full_name"): "A"})
    assert len(p.apply) == 1 and p.conflicts[0][1] == "edit_on_deleted"
    rows = mk([("create", "employees", EMP, "*", None, None)])
    assert plan(rows, deleted={("employees", EMP)}).conflicts[0][1] == "create_on_deleted"


def test_rows_out_of_counter_order_are_sorted_and_plan_equals_in_order():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "full_name", "B", "C"),
               ("set", "employees", EMP, "nationality", None, "X")])
    v = {("employees", EMP, "full_name"): "A"}
    want = plan(rows, values=v)
    got = plan(list(reversed(rows)), values=v)
    assert got.status == READY and got.apply == want.apply and not got.conflicts
    assert [r["origin_counter"] for r in got.apply] == [1, 2, 3]


def test_repeat_of_identical_row_in_one_session_is_kept_once():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "nationality", None, "X")])
    p = plan([rows[0], rows[1], rows[0]], values={("employees", EMP, "full_name"): "A"})
    assert len(p.apply) == 2 and not p.duplicates


def test_same_counter_different_content_in_one_session_is_integrity_failure():
    a = mk([("set", "employees", EMP, "full_name", "A", "B")])
    b = mk([("set", "employees", EMP, "full_name", "A", "Z")])
    with pytest.raises(ChainBroken):
        plan(a + b)


def test_order_gap_still_refused_after_sorting():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B"), ("set", "employees", EMP, "full_name", "B", "C"),
               ("set", "employees", EMP, "nationality", None, "X")])
    with pytest.raises(ChainBroken):
        plan([rows[2], rows[0]])


@pytest.mark.parametrize("bad", [0, -1, True, "1", None])
def test_bad_counter_refused(bad):
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    rows[0]["origin_counter"] = bad
    with pytest.raises(ChainBroken):
        plan(rows)


def test_delete_of_record_already_deleted_on_main_is_a_noop_not_a_conflict():
    rows = mk([("delete", "employees", EMP, "*", None, None)])
    p = plan(rows, deleted={("employees", EMP)}, touched={("employees", EMP)})
    assert p.status == READY and p.already_deleted == rows and not p.apply and not p.conflicts


def test_second_delete_in_same_session_is_a_noop():
    rows = mk([("delete", "employees", EMP, "*", None, None), ("delete", "employees", EMP, "*", None, None)])
    p = plan(rows)
    assert p.apply == [rows[0]] and p.already_deleted == [rows[1]] and not p.conflicts


def test_delete_against_edit_still_conflicts_and_other_deletes_apply():
    rows = mk([("delete", "employees", EMP, "*", None, None)])
    p = plan(rows, touched={("employees", EMP)})
    assert [c[1] for c in p.conflicts] == ["delete_against_edit"] and not p.already_deleted


def test_unknown_op_is_refused_before_any_visibility_check():
    rows = mk([("foo", "employees", EMP, "basic_salary", "1", "2")])
    with pytest.raises(SyncLogError):
        plan(rows, role="assistant_manager")
    with pytest.raises(SyncLogError):
        plan(rows)
