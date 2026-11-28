from datetime import datetime, timedelta, timezone
import pytest
from app import sync_pairing as p

T0 = datetime(2026, 11, 28, 9, 0, tzinfo=timezone.utc)
CODE = "ABCD-2345"


def fresh(**k):
    return p.issue(T0, lifetime_minutes=k.pop("lifetime_minutes", 10), code=CODE)


def test_new_code_shape_and_alphabet():
    seen = set()
    for _ in range(200):
        c = p.new_code()
        assert len(c) == 9 and c[4] == "-" and all(ch in p.ALPHABET for ch in c.replace("-", ""))
        seen.add(c)
    assert len(seen) == 200
    assert not set("0O1IL") & set(p.ALPHABET)


def test_record_never_holds_the_code():
    _, rec = fresh()
    assert "ABCD" not in repr(rec) and "2345" not in repr(rec) and len(rec.code_hash) == 64


def test_right_code_works_once_with_any_case_and_separators():
    _, rec = fresh()
    r, rec2 = p.check_code(rec, " abcd 2345 ", T0 + timedelta(minutes=1), max_failures=5)
    assert r == p.OK and rec2.used
    assert p.check_code(rec2, CODE, T0 + timedelta(minutes=2), max_failures=5)[0] == p.USED


def test_wrong_codes_count_then_lock_even_the_right_one():
    _, rec = fresh()
    for i in range(3):
        r, rec = p.check_code(rec, "WXYZ-2222", T0, max_failures=3)
        assert r == p.WRONG and rec.failures == i + 1
    assert p.check_code(rec, CODE, T0, max_failures=3) == (p.LOCKED, rec)


def test_expiry_is_exact_and_does_not_count_a_failure():
    _, rec = fresh(lifetime_minutes=10)
    assert p.check_code(rec, CODE, T0 + timedelta(minutes=10) - timedelta(microseconds=1), max_failures=3)[0] == p.OK
    r, rec2 = p.check_code(rec, CODE, T0 + timedelta(minutes=10), max_failures=3)
    assert r == p.EXPIRED and rec2 == rec


@pytest.mark.parametrize("typed", [None, 5, "", "ABCD", "ABCD-234", "ABCD-23456", "ABCD-234O", "ÄBCD-2345", b"x"])
def test_malformed_typed_codes_are_just_wrong(typed):
    _, rec = fresh()
    r, rec2 = p.check_code(rec, typed, T0, max_failures=5)
    assert r == p.WRONG and rec2.failures == 1


def test_required_arguments_have_no_defaults_and_validate():
    with pytest.raises(TypeError):
        p.issue(T0)
    _, rec = fresh()
    with pytest.raises(TypeError):
        p.check_code(rec, CODE, T0)
    for bad in (0, -1, True, None, "10"):
        with pytest.raises(p.PairingError):
            p.issue(T0, lifetime_minutes=bad)
        with pytest.raises(p.PairingError):
            p.check_code(rec, CODE, T0, max_failures=bad)
    with pytest.raises(p.PairingError):
        p.issue(datetime(2026, 1, 1), lifetime_minutes=10)
    with pytest.raises(p.PairingError):
        p.issue(T0, lifetime_minutes=10, code="ABCD-0000")


def test_salts_differ_between_records():
    assert fresh()[1].salt != fresh()[1].salt and fresh()[1].code_hash != fresh()[1].code_hash


def test_corrupt_records_are_refused_on_read():
    from dataclasses import replace
    _, rec = fresh()
    for bad in (replace(rec, expires_at="tomorrow"), replace(rec, expires_at=None), replace(rec, failures=-1),
                replace(rec, failures=True), replace(rec, used=1), replace(rec, salt="zz" * 16),
                replace(rec, code_hash="ab"), replace(rec, scrypt_n=3), replace(rec, scrypt_n=2 ** 30), replace(rec, scrypt_n=2 ** 18), replace(rec, scrypt_n=2 ** 20), None, "x"):
        with pytest.raises(p.PairingError):
            p.check_code(bad, CODE, T0, max_failures=3)


def test_scrypt_cost_is_a_parameter_stored_in_the_record():
    _, rec = p.issue(T0, lifetime_minutes=10, code=CODE, scrypt_n=2 ** 15)
    assert rec.scrypt_n == 2 ** 15
    assert p.check_code(rec, CODE, T0, max_failures=3)[0] == p.OK
    for bad in (2 ** 13, 3 * 2 ** 14, True, "16384"):
        with pytest.raises(p.PairingError):
            p.issue(T0, lifetime_minutes=10, code=CODE, scrypt_n=bad)


def test_largest_allowed_cost_runs_and_larger_is_a_pairing_error_not_valueerror():
    _, rec = p.issue(T0, lifetime_minutes=10, code=CODE, scrypt_n=2 ** 17)
    assert p.check_code(rec, CODE, T0, max_failures=3)[0] == p.OK
    for n in (2 ** 18, 2 ** 20):
        with pytest.raises(p.PairingError):
            p.issue(T0, lifetime_minutes=10, code=CODE, scrypt_n=n)
