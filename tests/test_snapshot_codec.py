"""Portable tests for snapshot_codec (packet 1702).

Pytest, standard library + pytest only. No database, no network,
synthetic data only. Run: python -m pytest test_snapshot_codec.py -q
"""
import hashlib
import itertools
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.snapshot_codec import (
    Snapshot,
    SnapshotCodecError,
    decode_snapshot,
    digest_snapshot,
    encode_snapshot,
)

UID = "123e4567-e89b-42d3-a456-426614174000"
UID2 = "123e4567-e89b-42d3-a456-426614174001"


def enc(values, entity="employees", uid=UID):
    return encode_snapshot(entity, uid, values)


def craft(values_node, entity='"employees"', uid=None, version="1", extra=""):
    """Hand-build envelope bytes (possibly noncanonical) for decode tests."""
    uid = uid if uid is not None else '"%s"' % UID
    text = '{"entity":%s,"uid":%s,"values":%s,"version":%s%s}' % (
        entity,
        uid,
        values_node,
        version,
        extra,
    )
    return text.encode("utf-8")


def typed(tag, v_json):
    return '{"t":"%s","v":%s}' % (tag, v_json)


def one_value(tag, v_json, field="f"):
    return craft('{"%s":%s}' % (field, typed(tag, v_json)))


# ---------------------------------------------------------------- basics

def test_roundtrip_all_supported_types():
    values = {
        "none_field": None,
        "bool_field": True,
        "int_field": -42,
        "float_field": 1.5,
        "decimal_field": Decimal("1234.50"),
        "str_field": "hello",
        "date_field": date(2026, 1, 1),
        "datetime_field": datetime(2026, 1, 1, 12, 30, 15, 123456,
                                   tzinfo=timezone(timedelta(hours=5, minutes=30))),
    }
    snap = decode_snapshot(enc(values))
    assert isinstance(snap, Snapshot)
    assert snap.entity == "employees" and snap.uid == UID
    assert snap.values == values
    assert type(snap.values["bool_field"]) is bool
    assert type(snap.values["int_field"]) is int
    assert type(snap.values["float_field"]) is float
    assert type(snap.values["decimal_field"]) is Decimal
    assert type(snap.values["date_field"]) is date
    assert type(snap.values["datetime_field"]) is datetime


def test_decode_reencode_byte_identity():
    values = {"a": Decimal("-0.00"), "b": -0.0, "c": "x=y", "d": 7}
    raw = enc(values)
    assert enc(decode_snapshot(raw).values) == raw


def test_all_four_entities_accepted():
    for entity in ["departments", "employees", "leave_types", "leave_records"]:
        assert decode_snapshot(enc({"f": 1}, entity=entity)).entity == entity


def test_digest_matches_sha256_of_canonical_bytes():
    values = {"salary": Decimal("1234.50")}
    assert digest_snapshot("employees", UID, values) == \
        hashlib.sha256(enc(values)).hexdigest()


def test_deterministic_bytes_independent_of_insertion_order():
    # Explicit property iteration count: 5! = 120 permutations, counted
    # here and reported separately from the pytest node count.
    items = [("a", 1), ("b", "two"), ("c", Decimal("3.0")),
             ("d", None), ("e", date(2026, 2, 2))]
    iterations = 0
    results = set()
    for perm in itertools.permutations(items):
        iterations += 1
        results.add(enc(dict(perm)))
    assert iterations == 120
    assert len(results) == 1


# ---------------------------------------------------------------- text

def test_unicode_and_special_strings_preserved():
    samples = [
        "سنوي", "Annual", "年假", "mixed إجازة leave 休假",
        'quote " and \'', "line1\nline2\r\nline3", "back\\slash",
        "=1+1", "=DEPT()", "  leading and trailing  ", "tab\there",
        "emoji-free snowman ☃", "combining é", "ünïcödé",
    ]
    for text in samples:
        snap = decode_snapshot(enc({"f": text}))
        assert snap.values["f"] == text
        assert type(snap.values["f"]) is str


def test_unpaired_surrogates_rejected():
    with pytest.raises(SnapshotCodecError):
        enc({"f": "\ud800 lone"})
    with pytest.raises(SnapshotCodecError):
        enc({"bad\udfffname": 1})
    raw = one_value("str", '"\\ud800"')
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(raw)


def test_malformed_utf8_rejected_on_decode():
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(b"\xff\xfe{\x00")


# ------------------------------------------------------- type distinction

def test_types_cannot_collide():
    variants = [1, "1", Decimal("1"), True, 1.0, "True", None, "None",
                date(2026, 1, 1), "2026-01-01"]
    encoded = [enc({"f": v}) for v in variants]
    assert len(set(encoded)) == len(variants)
    digests = [digest_snapshot("employees", UID, {"f": v}) for v in variants]
    assert len(set(digests)) == len(variants)


def test_negative_zero_and_decimal_scale_preserved():
    assert enc({"f": -0.0}) != enc({"f": 0.0})
    assert decode_snapshot(enc({"f": -0.0})).values["f"].hex() == (-0.0).hex()
    forms = [Decimal("0"), Decimal("0.0"), Decimal("0.00"),
             Decimal("-0.00"), Decimal("1E+2"), Decimal("100")]
    encoded = [enc({"f": d}) for d in forms]
    assert len(set(encoded)) == len(forms)
    for original, raw in zip(forms, encoded):
        back = decode_snapshot(raw).values["f"]
        assert back.as_tuple() == original.as_tuple()


def test_datetime_offsets_and_microseconds_preserved():
    for offset in [timedelta(hours=-4), timedelta(0),
                   timedelta(hours=5, minutes=30), timedelta(hours=14)]:
        moment = datetime(2026, 10, 1, 8, 0, 0, 999999,
                          tzinfo=timezone(offset))
        back = decode_snapshot(enc({"f": moment})).values["f"]
        assert back == moment and back.utcoffset() == offset
        assert back.microsecond == 999999


def test_date_and_datetime_are_distinct():
    assert enc({"f": date(2026, 1, 1)}) != enc(
        {"f": datetime(2026, 1, 1, tzinfo=timezone.utc)})


# ---------------------------------------------------------------- rejects

class StrSub(str):
    pass


class IntSub(int):
    pass


class FloatSub(float):
    pass


class DecimalSub(Decimal):
    pass


class DateSub(date):
    pass


class DatetimeSub(datetime):
    pass


@pytest.mark.parametrize("bad", [
    [1, 2], {"nested": 1}, (1, 2), {1, 2}, b"bytes", object(),
    StrSub("x"), IntSub(3), FloatSub(1.5), DecimalSub("1.5"),
    DateSub(2026, 1, 1), DatetimeSub(2026, 1, 1, tzinfo=timezone.utc),
    bytearray(b"x"),
])
def test_unsupported_and_subclass_values_rejected(bad):
    with pytest.raises(SnapshotCodecError):
        enc({"f": bad})


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"),
    Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"),
])
def test_nonfinite_rejected(bad):
    with pytest.raises(SnapshotCodecError):
        enc({"f": bad})


def test_naive_datetime_rejected():
    with pytest.raises(SnapshotCodecError):
        enc({"f": datetime(2026, 1, 1, 12, 0, 0)})


def test_invalid_uid_and_entity_rejected():
    for bad_uid in [UID.upper(), "123e4567-e89b-12d3-a456-426614174000",
                    "123e4567e89b42d3a456426614174000", "", 42, None]:
        with pytest.raises(SnapshotCodecError):
            enc({"f": 1}, uid=bad_uid)
    for bad_entity in ["branches", "Employees", "", None, 7]:
        with pytest.raises(SnapshotCodecError):
            enc({"f": 1}, entity=bad_entity)


def test_field_shape_rejected_on_encode():
    with pytest.raises(SnapshotCodecError):
        enc({})
    with pytest.raises(SnapshotCodecError):
        enc({"id": 1})
    with pytest.raises(SnapshotCodecError):
        enc({"sync_uid": UID2})
    with pytest.raises(SnapshotCodecError):
        enc({"": 1})
    with pytest.raises(SnapshotCodecError):
        enc({"x" * 129: 1})
    with pytest.raises(SnapshotCodecError):
        enc({i: 1 for i in range(129)})
    with pytest.raises(SnapshotCodecError):
        encode_snapshot("employees", UID, [("f", 1)])


def test_field_boundaries_accepted():
    assert decode_snapshot(enc({"x" * 128: 1})).values == {"x" * 128: 1}
    many = {"field_%03d" % i: i for i in range(128)}
    assert decode_snapshot(enc(many)).values == many


def test_numeric_boundaries():
    big_int = int("9" * 1000)
    assert decode_snapshot(enc({"f": big_int})).values["f"] == big_int
    with pytest.raises(SnapshotCodecError):
        enc({"f": int("9" * 1001)})
    big_dec = Decimal("9" * 1000)
    assert decode_snapshot(enc({"f": big_dec})).values["f"] == big_dec
    with pytest.raises(SnapshotCodecError):
        enc({"f": Decimal("9" * 1001)})
    assert decode_snapshot(enc({"f": Decimal("1E+1000")})).values["f"] == Decimal("1E+1000")
    assert decode_snapshot(enc({"f": Decimal("1E-1000")})).values["f"] == Decimal("1E-1000")
    with pytest.raises(SnapshotCodecError):
        enc({"f": Decimal("1E+1001")})
    with pytest.raises(SnapshotCodecError):
        enc({"f": Decimal("1E-1001")})


# ------------------------------------------------------------ decode side

def test_decode_requires_bytes_and_rejects_empty_and_trailing():
    good = enc({"f": 1})
    for bad in [good.decode("utf-8"), bytearray(good), None, 123]:
        with pytest.raises(SnapshotCodecError):
            decode_snapshot(bad)
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(b"")
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(good + b"\n")
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(good + b" garbage")
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(b" " + good)


def test_decode_rejects_duplicate_keys_at_every_level():
    dup_top = ('{"entity":"employees","entity":"employees","uid":"%s",'
               '"values":{"f":{"t":"int","v":"1"}},"version":1}' % UID).encode()
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(dup_top)
    dup_typed = one_value("int", '"1"').replace(b'"t":"int"', b'"t":"int","t":"int"')
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(dup_typed)
    dup_values = craft('{"f":%s,"f":%s}' % (typed("int", '"1"'), typed("int", '"2"')))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(dup_values)


def test_decode_rejects_missing_and_extra_keys():
    good_values = '{"f":%s}' % typed("int", '"1"')
    missing = ('{"entity":"employees","uid":"%s","values":%s}'
               % (UID, good_values)).encode()
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(missing)
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft(good_values, extra=',"extra":1'))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft('{"f":{"t":"int"}}'))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft('{"f":{"t":"int","v":"1","x":0}}'))


@pytest.mark.parametrize("version", ['"1"', "1.0", "true", "2", "0", "null"])
def test_decode_rejects_bool_and_other_version_tricks(version):
    raw = craft('{"f":%s}' % typed("int", '"1"'), version=version)
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(raw)


def test_decode_rejects_unknown_tag_and_nonfinite_json():
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(one_value("blob", '"x"'))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft('{"f":%s}' % typed("int", '"1"'), version="NaN"))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft('{"f":%s}' % typed("int", '"1"'), version="Infinity"))


@pytest.mark.parametrize("tag,v_json", [
    ("int", '"-0"'), ("int", '"01"'), ("int", '"+1"'), ("int", '"1.0"'),
    ("int", "1"), ("int", '""'),
    ("float", '"0X1.8000000000000P+0"'), ("float", '"1.5"'),
    ("float", '"nan"'), ("float", '"inf"'),
    ("decimal", '"0:01:0"'), ("decimal", '"0:1:-0"'),
    ("decimal", '"2:1:0"'), ("decimal", '"0:1:1001"'),
    ("decimal", '"1.5"'),
    ("date", '"20260101"'), ("date", '"2026-13-01"'), ("date", '"2026-1-1"'),
    ("datetime", '"2026-01-01T00:00:00"'),
    ("datetime", '"2026-01-01T00:00:00Z"'),
    ("datetime", '"2026-01-01T00:00:00+24:00"'),
    ("bool", '"true"'), ("bool", "1"),
    ("null", "0"), ("null", '"null"'),
    ("str", "5"),
])
def test_decode_rejects_noncanonical_or_malformed_typed_values(tag, v_json):
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(one_value(tag, v_json))


def test_decode_rejects_noncanonical_envelope_forms():
    good = enc({"f": "سنوي"})
    spaced = json.dumps(json.loads(good), ensure_ascii=False).encode("utf-8")
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(spaced)
    escaped = good.replace("سنوي".encode("utf-8"),
                           b"\\u0633\\u0646\\u0648\\u064a")
    assert escaped != good
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(escaped)
    unsorted = ('{"version":1,"values":{"f":{"t":"int","v":"1"}},'
                '"uid":"%s","entity":"employees"}' % UID).encode()
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(unsorted)


def test_decode_rejects_invalid_uid_entity_and_fields():
    values = '{"f":%s}' % typed("int", '"1"')
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft(values, uid='"%s"' % UID.upper()))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft(values, entity='"branches"'))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft('{"id":%s}' % typed("int", '"1"')))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(craft("{}"))


# ----------------------------------------------------------------- bounds

def test_encoded_byte_limit_exact_boundary():
    base_len = len(enc({"data": ""}))
    pad = 262144 - base_len
    raw = enc({"data": "a" * pad})
    assert len(raw) == 262144
    assert decode_snapshot(raw).values["data"] == "a" * pad
    with pytest.raises(SnapshotCodecError):
        enc({"data": "a" * (pad + 1)})


def test_decode_byte_limit_enforced_before_parse():
    oversize = enc({"f": 1}) + b" " * 262144
    assert len(oversize) > 262144
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(oversize)


def test_hostile_nesting_is_a_controlled_failure():
    deep = b"[" * 10000 + b"]" * 10000
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(deep)
    nested_value = craft('{"f":%s}' % ('{"a":' * 2000 + "1" + "}" * 2000))
    with pytest.raises(SnapshotCodecError):
        decode_snapshot(nested_value)


def test_error_messages_do_not_echo_values():
    secret_like = "synthetic-secret-marker-1702"
    try:
        enc({"f": [secret_like]})
    except SnapshotCodecError as exc:
        assert secret_like not in str(exc)
    else:
        raise AssertionError("expected SnapshotCodecError")
    raw = one_value("str", '"%s"' % secret_like).replace(b'"str"', b'"bogus"')
    try:
        decode_snapshot(raw)
    except SnapshotCodecError as exc:
        assert secret_like not in str(exc)
    else:
        raise AssertionError("expected SnapshotCodecError")
