import re
import uuid
import pytest
from app import sync_uid_seed as s
from app.sync_uid_mixin import SyncUidError, UID_REGEX, canonical_uid


def test_deterministic_and_canonical_v4():
    a = s.leave_type_uid("annual")
    assert a == s.leave_type_uid("annual") == s.leave_type_uid("  annual ")
    assert re.match(UID_REGEX, a) and canonical_uid(a) == a and uuid.UUID(a).version == 4


def test_pinned_values_never_change():
    # Pinned: a change here means every install's seeded uid changes. Do not update after release.
    assert s.leave_type_uid("annual") == "9c0b8e77-3c90-49cb-9535-e147fb871494"
    assert s.department_uid("Front") == "773fc6a1-f71c-48ae-89ce-6dc8bdf1aed6"
    assert s.branch_uid(s.department_uid("Front"), "Maadi") == "256f42fd-86df-4350-ab77-18fc49e4d0a3"
    assert s.leave_type_uid("annual") != s.leave_type_uid("sick")
    assert s.leave_type_uid("annual") != s.department_uid("annual")


def test_branch_uid_depends_on_department_uid_and_name_only():
    d1, d2 = s.department_uid("Front"), s.department_uid("Kitchen")
    assert s.branch_uid(d1, "Maadi") == s.branch_uid(d1, "Maadi")
    assert s.branch_uid(d1, "Maadi") != s.branch_uid(d2, "Maadi")
    assert s.branch_uid(d1, "Maadi") != s.branch_uid(d1, "Nasr")
    assert s.branch_uid(d1, "Maadi") != s.branch_uid(d1, "maadi")


def test_nfc_equivalent_arabic_names_agree():
    assert s.department_uid("e\u0301") == s.department_uid("\u00e9")


@pytest.mark.parametrize("bad", [None, 5, b"x", "", "   ", "a\x1fb", "a\x00"])
def test_bad_key_refused(bad):
    with pytest.raises(SyncUidError):
        s.leave_type_uid(bad)


def test_proposed_entities_need_explicit_flag():
    with pytest.raises(SyncUidError):
        s.derive_uid("departments", "Front")
    with pytest.raises(SyncUidError):
        s.derive_uid("branches", "x", proposal=False)
    assert s.derive_uid("departments", "Front", proposal=True) == s.department_uid("Front")


def test_bad_entity_or_parent_refused():
    with pytest.raises(SyncUidError):
        s.derive_uid("employees", "x")
    with pytest.raises(SyncUidError):
        s.derive_uid("leave_types")
    with pytest.raises(SyncUidError):
        s.branch_uid("not-a-uid", "Maadi")
    with pytest.raises(SyncUidError):
        s.branch_uid(str(uuid.uuid4()).upper(), "Maadi")


def test_key_parts_cannot_collide_by_concatenation():
    assert s.derive_uid("branches", "ab", "c", proposal=True) != s.derive_uid("branches", "a", "bc", proposal=True)


def test_many_distinct_keys_no_collision():
    uids = {s.leave_type_uid(f"k{i}") for i in range(2000)}
    assert len(uids) == 2000
