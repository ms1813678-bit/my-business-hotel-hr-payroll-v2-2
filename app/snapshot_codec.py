"""Isolated canonical typed snapshot codec (candidate, packet 1702).

Standard library only. No app imports, no database, no filesystem or
network access, no pickle/eval/compression, no secret handling.

This implements a newly selected prototype contract (TASK.md, packet 1702,
baseline 5c06d3402ee47d6ffcd1af32adc648c069d27c76). It is not a business
policy, not a role/disclosure allowlist, and not an accepted live-sync
protocol. Decoder success grants no disclosure permission. The public
SHA-256 digest is not authentication or permission.

Canonical envelope (version 1), exactly these top-level keys:

    {"entity": <str>, "uid": <canonical lowercase UUID4 str>,
     "values": {<field>: {"t": <tag>, "v": <representation>}, ...},
     "version": 1}

Serialised as UTF-8 JSON with sorted keys, separators (",", ":"),
ensure_ascii=False and no trailing bytes. Exactly one byte encoding is
accepted for any supported value: decoding re-encodes the decoded
snapshot and rejects the input if the bytes differ.

Typed scalar representations (tag -> representation):
    "null"    -> None
    "bool"    -> JSON true/false
    "int"     -> canonical decimal string (no sign for zero, no leading
                 zeros, at most 1000 digits)
    "float"   -> float.hex() string (finite only; preserves -0.0)
    "decimal" -> "<sign>:<digits>:<exponent>" from Decimal.as_tuple()
                 (finite only; preserves sign of zero and scale;
                 at most 1000 digits, absolute exponent at most 1000)
    "str"     -> the string itself (codepoints preserved, never
                 normalised; unpaired surrogates rejected)
    "date"    -> date.isoformat() ("YYYY-MM-DD", exact date type)
    "datetime"-> datetime.isoformat() of a timezone-aware datetime
                 (offset and microseconds preserved; naive rejected)

Bounds (prototype technical limits from the task, not product policy):
at most 128 fields, field names 1..128 codepoints and never the
identity names "id"/"sync_uid", at most 262144 encoded bytes.
"""
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

__all__ = [
    "Snapshot",
    "SnapshotCodecError",
    "encode_snapshot",
    "decode_snapshot",
    "digest_snapshot",
]

_VERSION = 1
_ENTITIES = frozenset({"departments", "employees", "leave_types", "leave_records"})
_RESERVED_FIELDS = frozenset({"id", "sync_uid"})
_MAX_FIELDS = 128
_MAX_FIELD_CODEPOINTS = 128
_MAX_BYTES = 262144
_MAX_INT_DIGITS = 1000
_MAX_DECIMAL_DIGITS = 1000
_MAX_DECIMAL_EXPONENT = 1000
_TOP_KEYS = frozenset({"entity", "uid", "values", "version"})
_TYPED_KEYS = frozenset({"t", "v"})
_INT_RE = re.compile(r"^(0|-?[1-9][0-9]*)$")
_DECIMAL_RE = re.compile(r"^([01]):([0-9]+):(0|-?[1-9][0-9]*)$")
_TAGS = frozenset(
    {"null", "bool", "int", "float", "decimal", "str", "date", "datetime"}
)


class SnapshotCodecError(ValueError):
    """Controlled rejection. Messages are generic categories only and
    never echo a supplied value."""


@dataclass(frozen=True)
class Snapshot:
    """Immutable envelope identity returned by decode_snapshot.

    ``values`` is a plain dict built by the decoder (caller-owned after
    return, as the task permits); the envelope fields cannot be rebound.
    """

    entity: str
    uid: str
    values: dict


def _fail(category):
    raise SnapshotCodecError("snapshot codec: " + category)


def _check_text(value, category):
    """Reject unpaired surrogates (which are not valid UTF-8 text)."""
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            _fail(category)
    return value


def _canonical_uid(value):
    if type(value) is not str:
        _fail("invalid uid")
    _check_text(value, "invalid uid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        _fail("invalid uid")
    if parsed.version != 4 or str(parsed) != value:
        _fail("invalid uid")
    return value


def _check_entity(value):
    if type(value) is not str or value not in _ENTITIES:
        _fail("invalid entity")
    return value


def _check_field_name(name):
    if type(name) is not str:
        _fail("invalid field name")
    _check_text(name, "invalid field name")
    if not 1 <= len(name) <= _MAX_FIELD_CODEPOINTS:
        _fail("invalid field name")
    if name in _RESERVED_FIELDS:
        _fail("reserved field name")
    return name


def _encode_decimal(value):
    if not value.is_finite():
        _fail("non-finite decimal")
    sign, digits, exponent = value.as_tuple()
    if len(digits) > _MAX_DECIMAL_DIGITS:
        _fail("decimal too large")
    if type(exponent) is not int or abs(exponent) > _MAX_DECIMAL_EXPONENT:
        _fail("decimal exponent out of range")
    digit_text = "".join(str(d) for d in digits)
    return "%d:%s:%d" % (sign, digit_text, exponent)


def _encode_value(value):
    # bool must be tested before int (bool is an int subclass), and
    # datetime before date (datetime is a date subclass). Subclasses of
    # every supported type are rejected by the exact-type checks.
    if value is None:
        return {"t": "null", "v": None}
    if type(value) is bool:
        return {"t": "bool", "v": value}
    if type(value) is int:
        if abs(value) >= 10 ** _MAX_INT_DIGITS:
            _fail("integer too large")
        return {"t": "int", "v": str(value)}
    if type(value) is float:
        if not math.isfinite(value):
            _fail("non-finite float")
        return {"t": "float", "v": value.hex()}
    if type(value) is Decimal:
        return {"t": "decimal", "v": _encode_decimal(value)}
    if type(value) is str:
        _check_text(value, "invalid string value")
        return {"t": "str", "v": value}
    if type(value) is datetime:
        try:
            offset = value.utcoffset() if value.tzinfo is not None else None
        except (ValueError, TypeError, OverflowError):
            _fail("invalid datetime offset")
        if offset is None:
            _fail("naive datetime")
        if not -timedelta(hours=24) < offset < timedelta(hours=24):
            _fail("invalid datetime offset")
        return {"t": "datetime", "v": value.isoformat()}
    if type(value) is date:
        return {"t": "date", "v": value.isoformat()}
    _fail("unsupported value type")


def encode_snapshot(entity, uid, values):
    """Encode one snapshot to its canonical bytes.

    ``values`` must be an exact dict with 1..128 distinct, non-reserved
    field names mapping to flat typed scalars. Raises SnapshotCodecError
    (a ValueError) on any contract violation; never truncates.
    """
    _check_entity(entity)
    _canonical_uid(uid)
    if type(values) is not dict:
        _fail("values must be a mapping")
    if not 1 <= len(values) <= _MAX_FIELDS:
        _fail("field count out of range")
    typed = {}
    for name, value in values.items():
        _check_field_name(name)
        typed[name] = _encode_value(value)
    envelope = {"entity": entity, "uid": uid, "values": typed, "version": _VERSION}
    try:
        text = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        raw = text.encode("utf-8", "strict")
    except SnapshotCodecError:
        raise
    except (ValueError, UnicodeEncodeError, RecursionError):
        _fail("encoding failed")
    if len(raw) > _MAX_BYTES:
        _fail("encoded snapshot too large")
    return raw


def digest_snapshot(entity, uid, values):
    """Hex SHA-256 over the canonical bytes of the snapshot.

    An integrity digest only: not authentication, not permission.
    """
    return hashlib.sha256(encode_snapshot(entity, uid, values)).hexdigest()


def _object_no_duplicates(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            _fail("duplicate key")
        obj[key] = value
    return obj


def _reject_constant(_name):
    _fail("non-finite number")


def _decode_decimal(text):
    match = _DECIMAL_RE.match(text)
    if match is None:
        _fail("noncanonical decimal")
    sign_text, digit_text, exponent_text = match.groups()
    if len(digit_text) > _MAX_DECIMAL_DIGITS:
        _fail("decimal too large")
    if len(digit_text) > 1 and digit_text.startswith("0"):
        _fail("noncanonical decimal")
    if len(exponent_text.lstrip("-")) > 4:
        _fail("decimal exponent out of range")
    exponent = int(exponent_text)
    if abs(exponent) > _MAX_DECIMAL_EXPONENT:
        _fail("decimal exponent out of range")
    digits = tuple(int(ch) for ch in digit_text)
    value = Decimal((int(sign_text), digits, exponent))
    if not value.is_finite():
        _fail("non-finite decimal")
    if _encode_decimal(value) != text:
        _fail("noncanonical decimal")
    return value


def _decode_value(node):
    if type(node) is not dict or frozenset(node.keys()) != _TYPED_KEYS:
        _fail("malformed typed value")
    tag = node["t"]
    raw = node["v"]
    if type(tag) is not str or tag not in _TAGS:
        _fail("unknown type tag")
    if tag == "null":
        if raw is not None:
            _fail("malformed null value")
        return None
    if tag == "bool":
        if type(raw) is not bool:
            _fail("malformed bool value")
        return raw
    if tag == "int":
        if type(raw) is not str or _INT_RE.match(raw) is None:
            _fail("noncanonical integer")
        if len(raw.lstrip("-")) > _MAX_INT_DIGITS:
            _fail("integer too large")
        return int(raw)
    if tag == "float":
        if type(raw) is not str:
            _fail("malformed float value")
        try:
            value = float.fromhex(raw)
        except (ValueError, OverflowError):
            _fail("malformed float value")
        if not math.isfinite(value) or value.hex() != raw:
            _fail("noncanonical float")
        return value
    if tag == "decimal":
        if type(raw) is not str:
            _fail("malformed decimal value")
        return _decode_decimal(raw)
    if tag == "str":
        if type(raw) is not str:
            _fail("malformed string value")
        return _check_text(raw, "invalid string value")
    if tag == "date":
        if type(raw) is not str:
            _fail("malformed date value")
        try:
            value = date.fromisoformat(raw)
        except ValueError:
            _fail("malformed date value")
        if type(value) is not date or value.isoformat() != raw:
            _fail("noncanonical date")
        return value
    # tag == "datetime"
    if type(raw) is not str:
        _fail("malformed datetime value")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        _fail("malformed datetime value")
    # Python 3.11 fromisoformat normalizes subsecond-only UTC offsets to
    # zero. Parse the explicit ISO offset separately to preserve valid offsets.
    offset_match = re.search(r"([+-])(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{6}))?)?$", raw)
    if offset_match:
        sign, hours, minutes, seconds, micros = offset_match.groups()
        if int(hours) > 23 or int(minutes) > 59 or int(seconds or 0) > 59:
            _fail("invalid datetime offset")
        delta = timedelta(hours=int(hours), minutes=int(minutes),
                          seconds=int(seconds or 0), microseconds=int(micros or 0))
        if sign == "-":
            delta = -delta
        value = value.replace(tzinfo=timezone(delta))
    if type(value) is not datetime:
        _fail("malformed datetime value")
    offset = value.utcoffset() if value.tzinfo is not None else None
    if offset is None:
        _fail("naive datetime")
    if not -timedelta(hours=24) < offset < timedelta(hours=24):
        _fail("invalid datetime offset")
    if value.isoformat() != raw:
        _fail("noncanonical datetime")
    return value


def _check_no_surrogates(node):
    # Iterative traversal: hostile nesting must not exhaust the stack here.
    stack = [node]
    while stack:
        item = stack.pop()
        if type(item) is str:
            _check_text(item, "invalid string in envelope")
        elif type(item) is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)


def decode_snapshot(data):
    """Decode canonical bytes back to a Snapshot.

    Accepts exactly the bytes encode_snapshot can produce: any other
    input (oversize, malformed UTF-8/JSON, duplicate or extra keys,
    unknown version/tag, noncanonical representation, trailing input)
    is a controlled SnapshotCodecError. The byte limit is enforced
    before any JSON parsing.
    """
    if type(data) is not bytes:
        _fail("input must be bytes")
    if len(data) > _MAX_BYTES:
        _fail("encoded snapshot too large")
    if not data:
        _fail("empty input")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail("malformed utf-8")
    try:
        envelope = json.loads(
            text,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except SnapshotCodecError:
        raise
    except (ValueError, RecursionError, MemoryError):
        _fail("malformed json")
    _check_no_surrogates(envelope)
    if type(envelope) is not dict or frozenset(envelope.keys()) != _TOP_KEYS:
        _fail("malformed envelope")
    version = envelope["version"]
    if type(version) is not int or version != _VERSION:
        _fail("unknown version")
    entity = _check_entity(envelope["entity"])
    uid = _canonical_uid(envelope["uid"])
    raw_values = envelope["values"]
    if type(raw_values) is not dict or not 1 <= len(raw_values) <= _MAX_FIELDS:
        _fail("field count out of range")
    values = {}
    for name, node in raw_values.items():
        _check_field_name(name)
        values[name] = _decode_value(node)
    snapshot = Snapshot(entity=entity, uid=uid, values=values)
    # Canonicality: exactly one accepted byte encoding per snapshot.
    if encode_snapshot(snapshot.entity, snapshot.uid, snapshot.values) != data:
        _fail("noncanonical encoding")
    return snapshot
