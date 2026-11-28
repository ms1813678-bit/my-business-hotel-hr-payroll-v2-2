import pytest
from app.sync_apply import RETURNED  # noqa: F401
from app.sync_apply_ops import plan_to_ops
from app.sync_change_log import SyncLogError
from app.sync_rules import ACCEPT, ADMIT, REFUSE, REJECT, resolve_conflict, resolve_duplicate_code, restore_guard
from tests.test_sync_apply import DEV, EMP, mk, plan


def g(**k):
    base = dict(main_epoch=2, device_epoch=2, main_known_counter=5, device_last_counter=5)
    base.update(k)
    return restore_guard(**base)


def test_guard_admits_matching_history_including_device_behind_in_counter():
    assert (g().decision, g().reason) == (ADMIT, "ok")
    assert g(device_last_counter=3).decision == ADMIT


def test_guard_refuses_newer_epoch_counter_ahead_and_older_epoch_in_that_order():
    assert (g(device_epoch=3).decision, g(device_epoch=3).reason) == (REFUSE, "device_epoch_newer")
    assert g(device_last_counter=6).reason == "device_counter_ahead"
    assert g(device_epoch=1).reason == "device_epoch_older_decision_needed"
    assert g(device_epoch=3, device_last_counter=9).reason == "device_epoch_newer"
    assert g(device_epoch=1, device_last_counter=9).reason == "device_counter_ahead"


@pytest.mark.parametrize("k", ["main_epoch", "device_epoch", "main_known_counter", "device_last_counter"])
@pytest.mark.parametrize("bad", [-1, True, None, "1", 1.0])
def test_guard_bad_numbers_raise(k, bad):
    with pytest.raises(SyncLogError):
        g(**{k: bad})


def conflicts_for(rows, **kw):
    return plan_to_ops(plan(rows, **kw), device_id=DEV).conflicts


def test_accept_same_field_gives_the_set_and_reject_gives_nothing():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    c = conflicts_for(rows, values={("employees", EMP, "full_name"): "Z"})[0]
    (op,) = resolve_conflict(c, ACCEPT)
    assert (op.kind, op.field, op.new_value, op.origin_counter) == ("set", "full_name", "B", 1)
    assert resolve_conflict(c, REJECT) == ()


def test_accept_delete_against_edit_gives_the_delete():
    rows = mk([("delete", "employees", EMP, "*", None, None)])
    c = conflicts_for(rows, touched={("employees", EMP)})[0]
    assert [o.kind for o in resolve_conflict(c, ACCEPT)] == ["delete"]


def test_accept_edit_on_deleted_or_create_on_deleted_is_refused_reject_is_fine():
    e = conflicts_for(mk([("set", "employees", EMP, "full_name", "A", "B")]), deleted={("employees", EMP)})[0]
    c = conflicts_for(mk([("create", "employees", EMP, "*", None, None)]), deleted={("employees", EMP)})[0]
    for x in (e, c):
        with pytest.raises(SyncLogError):
            resolve_conflict(x, ACCEPT)
        assert resolve_conflict(x, REJECT) == ()


def test_bad_decision_reason_or_mismatched_kind():
    rows = mk([("set", "employees", EMP, "full_name", "A", "B")])
    c = dict(conflicts_for(rows, values={("employees", EMP, "full_name"): "Z"})[0])
    for d in ("yes", None, ""):
        with pytest.raises(SyncLogError):
            resolve_conflict(c, d)
    with pytest.raises(SyncLogError):
        resolve_conflict({**c, "reason": "nope"}, ACCEPT)
    with pytest.raises(SyncLogError):
        resolve_conflict({**c, "kind": "delete"}, ACCEPT)


def test_duplicate_code_free_is_kept_taken_gets_next_after_highest():
    assert resolve_duplicate_code("MY-0007", {"MY-0001", "MY-0002"}, 2) == ("MY-0007", False, 7)
    assert resolve_duplicate_code("MY-0002", {"MY-0001", "MY-0002"}, 2) == ("MY-0003", True, 3)
    assert resolve_duplicate_code("MY-0002", {"MY-0002", "MY-0009"}, 4) == ("MY-0010", True, 10)
    assert resolve_duplicate_code("MY-0002", {"MY-0002"}, 12) == ("MY-0013", True, 13)
    assert resolve_duplicate_code("MY-12345", set(), 3) == ("MY-12345", False, 12345)


def test_free_code_above_counter_advances_counter_and_free_code_below_keeps_it():
    assert resolve_duplicate_code("MY-0050", set(), 1).counter_after == 50
    assert resolve_duplicate_code("MY-0003", {"MY-0001"}, 20).counter_after == 20


def test_codes_compare_by_number_not_string():
    assert resolve_duplicate_code("MY-00007", {"MY-0007"}, 7) == ("MY-0008", True, 8)
    assert resolve_duplicate_code("MY-00007", set(), 3) == ("MY-0007", True, 7)
    assert resolve_duplicate_code("MY-0007", {"MY-00007"}, 7).corrected is True


def test_malformed_conflicts_raise_synclogerror_only():
    good = {"reason": "same_field_changed", "kind": "set", "entity": "employees", "uid": EMP, "field": "full_name",
            "old_value": "A", "new_value": "B", "origin_device_id": DEV, "origin_counter": 1}
    for bad in (None, 5, [], "x", {}, {"reason": "same_field_changed"}, {**good, "kind": "delete"},
                {k: v for k, v in good.items() if k != "uid"}, {**good, "uid": 5}, {**good, "new_value": 5},
                {**good, "origin_counter": True}, {**good, "origin_counter": 0}):
        with pytest.raises(SyncLogError):
            resolve_conflict(bad, ACCEPT)
    assert len(resolve_conflict(good, ACCEPT)) == 1
    assert resolve_conflict({"reason": "same_field_changed"}, REJECT) == ()


@pytest.mark.parametrize("w", ["my-0001", "MY-1", "X", None, "MY-00a1"])
def test_duplicate_code_bad_wanted_refused(w):
    with pytest.raises(SyncLogError):
        resolve_duplicate_code(w, set(), 0)


def test_duplicate_code_bad_inputs():
    with pytest.raises(SyncLogError):
        resolve_duplicate_code("MY-0001", {"bad"}, 0)
    for bad in (-1, "5", None, True, 1.0):
        with pytest.raises(SyncLogError):
            resolve_duplicate_code("MY-0001", set(), bad)


@pytest.mark.parametrize("bad", [None, 5, "MY-0001", b"x", object()])
def test_codes_in_use_must_be_a_collection(bad):
    with pytest.raises(SyncLogError):
        resolve_duplicate_code("MY-0002", bad, 0)
