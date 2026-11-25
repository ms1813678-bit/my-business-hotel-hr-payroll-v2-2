"""Strict parser for the API-set schema namespace section (.apiset bytes).

NAMESPACE VERSION 6 ONLY. Versions 2, 4 and every other version raise
UnsupportedVersionError: the previously remembered v2/v4 layouts were wrong
against the pinned phnt header (v2 entry is 12 bytes there, not 16; v2 value
records are 16 bytes behind a Count header, not 8; v4 entry is 24 bytes, not
28; v4 value records sit behind a Flags+Count array header) and no
source-backed correction with tests was possible for them. See
docs/LAYOUT-V6.md.

LAYOUT STATUS: SOURCE_DERIVED_UNTESTED. Every v6 offset below is derived
from the pinned phnt header ntpebteb.h @ 53fbbdc5b5d2b08761db1c7b26bfa8c820924356
(SHA256 9869d45323706a6bf7cb3ed168b4ca6560aef194f1c6a4d37aaad74a356ad248),
cited by line in docs/LAYOUT-V6.md. phnt definitions are reverse-engineered
and private, NOT a Microsoft ABI guarantee, and nothing here has been tested
against real apisetschema.dll bytes.

Offsets in the namespace section are relative to the start of the section
bytes: the header comments mark string/table offsets "from schema base" and
the v6 EntryOffset/HashOffset "from this struct base"; the namespace struct
sits at the section start, so both bases are offset 0 here.

Validated on every parse: all offsets, count multiplications, entry/table/
string extents, UTF-16 2-byte alignment and strict decode, truncation,
HashedLength <= NameLength, HashedLength evenness, hash-table sorted order,
hash index range, index permutation, and hash/entry correspondence by
recomputation. The hash function is the review-described reverse-engineered
one: rolling u32 starting at 0, multiply by the header HashFactor, add the
ASCII-lowercased character, over the name prefix of HashedLength bytes. Any
character outside ASCII in that prefix is an explicit refusal (unsupported
Unicode casing): NamespaceFormatError, never an approximation. Malformed
rows are never silently skipped.

Overlap policy: structural regions (header, entry table, hash table, value
tables) may not overlap anything. String regions may be reused ONLY as a
validated exact extent with identical decoded text (interned read-only
string table), recorded as a finding; partial string overlap and any
string/structural overlap are format errors.

Flags: raw header/entry/value flags are preserved in findings and evidence.
Bits named in the pinned header (namespace SEALED|HOST_EXTENSION, entry
SEALED|EXTENSION) are known; any other bit refuses consumable mapping
(header-wide or per-entry). The pinned header names no v6 value flags, so
any non-zero value flags refuse that entry's consumable mapping.

Standard library only. Read-only: accepts the section as bytes.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import LAYOUT_STATUS


class NamespaceFormatError(ValueError):
    """Section bytes violate the source-derived layout or a hard bound."""


class UnsupportedVersionError(NamespaceFormatError):
    """The namespace version is not 6; this parser refuses to interpret it."""


class BoundsExceeded(NamespaceFormatError):
    """A declared count or length exceeded a configured hard bound."""


SUPPORTED_VERSIONS = (6,)

# v6 layout, derived from the pinned phnt header (docs/LAYOUT-V6.md cites
# every line). API_SET_NAMESPACE: 7 x ULONG. API_SET_NAMESPACE_ENTRY:
# 6 x ULONG. API_SET_VALUE_ENTRY: 5 x ULONG. API_SET_HASH_ENTRY: 2 x ULONG.
V6_HEADER_SIZE = 28
V6_ENTRY_SIZE = 24
V6_VALUE_SIZE = 20
V6_HASH_SIZE = 8

# Flag bits named by the pinned header: API_SET_SCHEMA_FLAGS_SEALED 0x1 and
# API_SET_SCHEMA_FLAGS_HOST_EXTENSION 0x2; API_SET_SCHEMA_ENTRY_FLAGS_SEALED
# 0x1 and API_SET_SCHEMA_ENTRY_FLAGS_EXTENSION 0x2. The header names no v6
# value-entry flag bits. Any bit outside these masks is unknown and refuses
# consumable mapping.
NAMESPACE_FLAGS_KNOWN_MASK = 0x00000003
ENTRY_FLAGS_KNOWN_MASK = 0x00000003
VALUE_FLAGS_KNOWN_MASK = 0x00000000
NAMESPACE_FLAG_SEALED = 0x1
NAMESPACE_FLAG_HOST_EXTENSION = 0x2
ENTRY_FLAG_SEALED = 0x1
ENTRY_FLAG_EXTENSION = 0x2


@dataclass(frozen=True)
class Limits:
    max_section_bytes: int = 4 * 1024 * 1024
    max_entries: int = 16384
    max_values_per_entry: int = 256
    max_string_bytes: int = 8192


@dataclass(frozen=True)
class ValueRecord:
    flags: int                    # raw v6 value-entry Flags field
    name: str                     # "" means no context name (default slot)
    name_offset: int
    name_length: int
    value: str                    # "" means empty host string
    value_offset: int
    value_length: int


@dataclass(frozen=True)
class NamespaceEntry:
    name: str
    flags: int                    # raw v6 namespace-entry Flags field
    name_offset: int
    name_length: int
    hashed_len: int               # raw HashedLength field (bytes)
    values: Tuple[ValueRecord, ...]


@dataclass
class NamespaceParse:
    version: int
    layout_status: str
    section_size: int             # full original section length
    section_sha256: str           # SHA256 of the FULL original section bytes
    namespace_size: int = 0       # validated header Size: the parse bound
    namespace_sha256: str = ""    # SHA256 of data[:namespace_size] only
    header_flags: int = 0
    hash_factor: int = 0
    entries: List[NamespaceEntry] = field(default_factory=list)
    findings: List[dict] = field(default_factory=list)


class _RegionBook:
    """Tracks occupied [start, end) extents under the two-class overlap
    policy: structural regions tolerate no overlap at all; string regions
    tolerate only a validated exact reuse (identical extent AND identical
    decoded text), which is recorded as a shared-extent finding."""

    def __init__(self, size: int):
        self.size = size
        self.structural: List[Tuple[int, int, str]] = []
        self.strings: List[Tuple[int, int, str, str]] = []

    def _bounds(self, off: int, length: int, what: str) -> bool:
        if off < 0 or length < 0 or off + length > self.size:
            raise NamespaceFormatError(
                "%s: extent [%#x, %#x) outside section of %#x bytes"
                % (what, off, off + length, self.size))
        return length > 0

    def claim_structural(self, off: int, length: int, what: str) -> None:
        if not self._bounds(off, length, what):
            return
        for a, b, other in self.structural:
            if off < b and a < off + length:
                raise NamespaceFormatError(
                    "%s: extent [%#x, %#x) overlaps structural %s [%#x, %#x)"
                    % (what, off, off + length, other, a, b))
        for a, b, other, _ in self.strings:
            if off < b and a < off + length:
                raise NamespaceFormatError(
                    "%s: structural extent [%#x, %#x) overlaps string %s [%#x, %#x)"
                    % (what, off, off + length, other, a, b))
        self.structural.append((off, off + length, what))

    def claim_string(self, off: int, length: int, what: str,
                     text: str) -> Optional[dict]:
        """Claim a string extent. Returns a shared-extent finding when this
        is a validated exact reuse; raises on any other overlap."""
        if not self._bounds(off, length, what):
            return None
        for a, b, other in self.structural:
            if off < b and a < off + length:
                raise NamespaceFormatError(
                    "%s: string extent [%#x, %#x) overlaps structural %s [%#x, %#x)"
                    % (what, off, off + length, other, a, b))
        for a, b, other, otext in self.strings:
            if off < b and a < off + length:
                if a == off and b == off + length and otext == text:
                    return {"kind": "string-extent-shared",
                            "offset": off, "length": length,
                            "claimants": [other, what],
                            "note": "validated exact immutable string-range "
                                    "reuse (identical extent and decoded "
                                    "text; interned read-only string table)"}
                raise NamespaceFormatError(
                    "%s: string extent [%#x, %#x) partially overlaps string "
                    "%s [%#x, %#x)" % (what, off, off + length, other, a, b))
        self.strings.append((off, off + length, what, text))
        return None


def _u32(d: bytes, o: int, what: str) -> int:
    if o < 0 or o + 4 > len(d):
        raise NamespaceFormatError("%s: u32 at %#x truncated" % (what, o))
    return struct.unpack_from("<I", d, o)[0]


def _read_utf16(d: bytes, book: _RegionBook, off: int, length: int,
                what: str, limits: Limits,
                findings: List[dict]) -> str:
    if length > limits.max_string_bytes:
        raise BoundsExceeded("%s: string length %d exceeds bound %d"
                             % (what, length, limits.max_string_bytes))
    if length == 0:
        return ""
    if off % 2 or length % 2:
        raise NamespaceFormatError(
            "%s: UTF-16 offset/length not 2-byte aligned (off %#x, len %#x)"
            % (what, off, length))
    raw = d[off:off + length]
    try:
        text = raw.decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as exc:
        raise NamespaceFormatError("%s: UTF-16LE decode failed: %s" % (what, exc))
    if "\x00" in text:
        raise NamespaceFormatError("%s: embedded NUL in UTF-16 string" % what)
    shared = book.claim_string(off, length, what, text)
    if shared:
        findings.append(shared)
    return text


def _entry_count(count: int, limits: Limits, what: str) -> int:
    if count > limits.max_entries:
        raise BoundsExceeded("%s: count %d exceeds bound %d" % (what, count, limits.max_entries))
    return count


def _value_count(count: int, limits: Limits, what: str) -> int:
    if count > limits.max_values_per_entry:
        raise BoundsExceeded("%s: value count %d exceeds bound %d"
                             % (what, count, limits.max_values_per_entry))
    return count


def _ascii_lower_unit(ch: str, what: str) -> int:
    """ASCII-only lowercase for the hash recompute. Any non-ASCII character
    is an explicit refusal: Unicode casing semantics are not established by
    the source and are never approximated."""
    o = ord(ch)
    if 0x41 <= o <= 0x5A:
        return o + 0x20
    if o > 0x7F:
        raise NamespaceFormatError(
            "%s: non-ASCII character U+%04X in hashed name prefix; "
            "unsupported Unicode casing, refusing hash validation" % (what, o))
    return o


def _recompute_hash(name: str, hashed_len: int, hash_factor: int,
                    what: str) -> int:
    """Review-described rolling u32 hash: start 0, multiply by the header
    HashFactor, add the ASCII-lowercased character, over the name prefix of
    HashedLength bytes. Reverse-engineered semantics (per the 0705 review's
    description of Chappell's documentation), not a Microsoft guarantee."""
    if hashed_len % 2:
        raise NamespaceFormatError(
            "%s: hashed length %d not 2-byte aligned" % (what, hashed_len))
    h = 0
    for ch in name[:hashed_len // 2]:
        h = (h * hash_factor + _ascii_lower_unit(ch, what)) & 0xFFFFFFFF
    return h


def parse_namespace(data: bytes, limits: Optional[Limits] = None) -> NamespaceParse:
    """Parse .apiset section bytes as a v6 namespace. Raises
    NamespaceFormatError on any malformation, BoundsExceeded on any exceeded
    bound, and UnsupportedVersionError for every version other than 6
    (including 2 and 4, whose remembered layouts were wrong)."""
    limits = limits or Limits()
    if len(data) == 0:
        raise NamespaceFormatError("empty section")
    if len(data) > limits.max_section_bytes:
        raise BoundsExceeded("section exceeds %d byte bound" % limits.max_section_bytes)
    if len(data) < 4:
        raise NamespaceFormatError("section too small for a version field")
    version = _u32(data, 0, "namespace header")
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedVersionError(
            "namespace version %d refused: only version 6 is supported, with "
            "a layout derived from the pinned phnt header; v2/v4 remembered "
            "layouts were wrong against that header and were removed, and no "
            "other version is ever guessed" % version)
    findings: List[dict] = []

    if len(data) < V6_HEADER_SIZE:
        raise NamespaceFormatError("section of %d bytes too small for the "
                                   "28-byte v6 header" % len(data))
    size = _u32(data, 4, "v6 header")
    if size < V6_HEADER_SIZE:
        raise NamespaceFormatError(
            "v6 header Size %#x is below the %d-byte header minimum"
            % (size, V6_HEADER_SIZE))
    if size > len(data):
        raise NamespaceFormatError(
            "v6 header Size %#x exceeds the section length %#x"
            % (size, len(data)))
    # The declared Size is the bound for EVERY subsequent structural, string
    # and hash read and region. Bytes at or beyond Size are never addressable:
    # all reads below go through `ns`, which is sliced to Size.
    section = data
    ns = bytes(data[:size])
    data = ns
    book = _RegionBook(size)
    book.claim_structural(0, V6_HEADER_SIZE, "v6 header")
    flags = _u32(data, 8, "v6 header")
    count = _entry_count(_u32(data, 12, "v6 header"), limits, "v6 entry count")
    entry_off = _u32(data, 16, "v6 header")
    hash_off = _u32(data, 20, "v6 header")
    hash_factor = _u32(data, 24, "v6 header")

    if size != len(section):
        tail = section[size:]
        findings.append({"kind": "size-field-trailing-bytes",
                         "trailing_length": len(tail),
                         "trailing_all_zero": not any(tail),
                         "trailing_sha256": hashlib.sha256(tail).hexdigest(),
                         "detail": "v6 header Size %#x is shorter than the "
                                   "section length %#x; the %d trailing bytes "
                                   "are outside the namespace, never read, "
                                   "never addressable"
                                   % (size, len(section), len(tail))})
    findings.append({"kind": "header-flags-raw",
                     "detail": "v6 header flags %#x preserved raw; known bits "
                               "SEALED|HOST_EXTENSION mask %#x"
                               % (flags, NAMESPACE_FLAGS_KNOWN_MASK)})
    if flags & NAMESPACE_FLAG_HOST_EXTENSION:
        findings.append({"kind": "header-host-extension",
                         "blocks_mapping": True,
                         "detail": "v6 header flags %#x carry HOST_EXTENSION "
                                   "(0x2): the namespace may be an extension "
                                   "rather than an unconditional base "
                                   "namespace and the semantics are not "
                                   "independently established; consumable "
                                   "mapping refused, raw flags retained"
                                   % flags})
    if flags & NAMESPACE_FLAG_SEALED:
        findings.append({"kind": "header-sealed-unestablished",
                         "blocks_mapping": True,
                         "detail": "v6 header flags %#x carry SEALED (0x1): "
                                   "the pinned header (line 85) names the bit "
                                   "but gives no meaning, and no fetched "
                                   "source establishes it; consumable "
                                   "mapping refused, raw flags retained"
                                   % flags})
    if flags & ~NAMESPACE_FLAGS_KNOWN_MASK:
        findings.append({"kind": "header-flags-unknown-bits",
                         "blocks_mapping": True,
                         "detail": "v6 header flags %#x carry bits outside the "
                                   "pinned-header mask %#x; unknown semantics, "
                                   "consumable mapping refused" % (flags, NAMESPACE_FLAGS_KNOWN_MASK)})

    book.claim_structural(entry_off, count * V6_ENTRY_SIZE, "v6 entry table")

    entries: List[NamespaceEntry] = []
    for i in range(count):
        base = entry_off + i * V6_ENTRY_SIZE
        eflags = _u32(data, base, "v6 entry %d" % i)
        name_off = _u32(data, base + 4, "v6 entry %d" % i)
        name_len = _u32(data, base + 8, "v6 entry %d" % i)
        hashed_len = _u32(data, base + 12, "v6 entry %d" % i)
        value_off = _u32(data, base + 16, "v6 entry %d" % i)
        value_count = _value_count(_u32(data, base + 20, "v6 entry %d" % i),
                                   limits, "v6 entry %d" % i)
        if hashed_len > name_len:
            raise NamespaceFormatError(
                "v6 entry %d: hashed length %d exceeds name length %d"
                % (i, hashed_len, name_len))
        if hashed_len % 2:
            raise NamespaceFormatError(
                "v6 entry %d: hashed length %d not 2-byte aligned" % (i, hashed_len))
        book.claim_structural(value_off, value_count * V6_VALUE_SIZE,
                              "v6 entry %d value table" % i)
        name = _read_utf16(data, book, name_off, name_len,
                           "v6 entry %d name" % i, limits, findings)
        values = []
        for j in range(value_count):
            vb = value_off + j * V6_VALUE_SIZE
            vflags = _u32(data, vb, "v6 entry %d value %d" % (i, j))
            vn_off = _u32(data, vb + 4, "v6 entry %d value %d" % (i, j))
            vn_len = _u32(data, vb + 8, "v6 entry %d value %d" % (i, j))
            voff = _u32(data, vb + 12, "v6 entry %d value %d" % (i, j))
            vlen = _u32(data, vb + 16, "v6 entry %d value %d" % (i, j))
            vname = _read_utf16(data, book, vn_off, vn_len,
                                "v6 entry %d value %d name" % (i, j), limits, findings)
            value = _read_utf16(data, book, voff, vlen,
                                "v6 entry %d value %d host" % (i, j), limits, findings)
            values.append(ValueRecord(vflags, vname, vn_off, vn_len,
                                      value, voff, vlen))
        entries.append(NamespaceEntry(name, eflags, name_off, name_len,
                                      hashed_len, tuple(values)))

    _validate_hash_table(data, book, count, hash_off, hash_factor, entries,
                         findings)

    return NamespaceParse(
        version=6,
        layout_status=LAYOUT_STATUS,
        section_size=len(section),
        section_sha256=hashlib.sha256(section).hexdigest(),
        namespace_size=size,
        namespace_sha256=hashlib.sha256(ns).hexdigest(),
        header_flags=flags,
        hash_factor=hash_factor,
        entries=entries,
        findings=findings,
    )


def _validate_hash_table(data: bytes, book: _RegionBook, count: int,
                         hash_off: int, hash_factor: int,
                         entries: List[NamespaceEntry],
                         findings: List[dict]) -> None:
    """Source-described hash/index/order validation. Any inconsistency is a
    loud format error; an absent table blocks consumable mapping."""
    if count == 0:
        return
    if hash_off == 0:
        findings.append({"kind": "hash-table-absent",
                         "blocks_mapping": True,
                         "detail": "v6 hash offset is zero with %d entries; "
                                   "hash/index/order validation impossible, "
                                   "consumable mapping refused" % count})
        return
    book.claim_structural(hash_off, count * V6_HASH_SIZE, "v6 hash table")
    records = []
    for i in range(count):
        h = _u32(data, hash_off + i * V6_HASH_SIZE, "v6 hash entry %d" % i)
        idx = _u32(data, hash_off + i * V6_HASH_SIZE + 4, "v6 hash entry %d" % i)
        records.append((h, idx))
    for i in range(1, count):
        if records[i][0] < records[i - 1][0]:
            raise NamespaceFormatError(
                "v6 hash table not sorted at row %d (%#x after %#x)"
                % (i, records[i][0], records[i - 1][0]))
    for i, (h, idx) in enumerate(records):
        if idx >= count:
            raise NamespaceFormatError(
                "v6 hash entry %d: index %d out of range (count %d)"
                % (i, idx, count))
    if sorted(idx for _, idx in records) != list(range(count)):
        raise NamespaceFormatError(
            "v6 hash indices are not a permutation of 0..%d" % (count - 1))
    for i, (h, idx) in enumerate(records):
        entry = entries[idx]
        recomputed = _recompute_hash(entry.name, entry.hashed_len,
                                     hash_factor, "v6 hash entry %d" % i)
        if recomputed != h:
            raise NamespaceFormatError(
                "v6 hash entry %d: stored hash %#010x != recomputed %#010x "
                "for entry %d (%r); bytes disagree with the described hash "
                "semantics, refusing" % (i, h, recomputed, idx, entry.name))
    findings.append({
        "kind": "hash-validation",
        "detail": "recomputed all %d hash entries per the review-described "
                  "semantics (rolling u32 from 0, multiply by header "
                  "HashFactor %#x, add ASCII-lowercased character, over the "
                  "name prefix of HashedLength bytes); sorted order, index "
                  "range/permutation and entry correspondence all validated. "
                  "Semantics are reverse-engineered (per the 0705 review of "
                  "Chappell's documentation), not proven against real bytes; "
                  "fixture agreement is not semantic proof"
                  % (count, hash_factor)})
