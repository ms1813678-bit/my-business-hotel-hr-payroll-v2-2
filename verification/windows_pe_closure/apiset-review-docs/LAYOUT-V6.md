# Source-derived v6 layout table

Every row cites the pinned phnt header ntpebteb.h @
53fbbdc5b5d2b08761db1c7b26bfa8c820924356 (SHA256 9869d453...d248). ULONG = 4
bytes, little-endian. All offsets are from the section start: string/table
offsets are "from schema base" and the v6 EntryOffset/HashOffset are "from
this struct base"; the namespace struct sits at the section start, so both
bases coincide at offset 0.

## Constants

| Name | Value | Header lines |
|---|---|---|
| API_SET_SECTION_NAME | ".apiset" | 76 |
| API_SET_SCHEMA_VERSION_V6 | 0x00000006 | 81 (V2/V4 at 79/80) |
| API_SET_SCHEMA_FLAGS_SEALED / _HOST_EXTENSION | 0x1 / 0x2 | 85-86 |
| API_SET_SCHEMA_ENTRY_FLAGS_SEALED / _EXTENSION | 0x1 / 0x2 | 89-90 |

## API_SET_NAMESPACE (v6 header), 7 x ULONG = 28 bytes, lines 192-201

| Offset | Field | Line |
|---|---|---|
| 0 | Version (== 6) | 194 |
| 4 | Size | 195 |
| 8 | Flags (API_SET_SCHEMA_FLAGS_*) | 196 |
| 12 | Count | 197 |
| 16 | EntryOffset -> API_SET_NAMESPACE_ENTRY[Count] | 198 |
| 20 | HashOffset -> API_SET_HASH_ENTRY[Count] | 199 |
| 24 | HashFactor | 200 |

## API_SET_NAMESPACE_ENTRY, 6 x ULONG = 24 bytes, lines 174-182

| Offset | Field | Line |
|---|---|---|
| 0 | Flags (API_SET_SCHEMA_ENTRY_FLAGS_*) | 176 |
| 4 | NameOffset -> WCHAR[NameLength/2] | 177 |
| 8 | NameLength (bytes) | 178 |
| 12 | HashedLength (bytes) | 179 |
| 16 | ValueOffset -> API_SET_VALUE_ENTRY[ValueCount] | 180 |
| 20 | ValueCount | 181 |

## API_SET_VALUE_ENTRY, 5 x ULONG = 20 bytes, lines 164-171

| Offset | Field | Line |
|---|---|---|
| 0 | Flags (no bits named in the pinned header) | 166 |
| 4 | NameOffset -> WCHAR[NameLength/2] (context name; empty = default) | 167 |
| 8 | NameLength (bytes) | 168 |
| 12 | ValueOffset -> WCHAR[ValueLength/2] (host) | 169 |
| 16 | ValueLength (bytes) | 170 |

## API_SET_HASH_ENTRY, 2 x ULONG = 8 bytes, lines 185-189

| Offset | Field | Line |
|---|---|---|
| 0 | Hash | 187 |
| 4 | Index (into the entry array) | 188 |

## Why v2/v4 support was removed (review vs pinned header)

The previous proposal's remembered v2/v4 layouts disagree with the pinned
header, so both versions are refused rather than guessed:

- v2 namespace entry: proposal 16 bytes with a data_len field; pinned header
  has 3 ULONGs (NameOffset, NameLength, DataOffset) = 12 bytes, lines 109-114.
- v2 values: proposal 8-byte records, count from data_len/8; pinned header
  has a Count-headed array (lines 102-106) of 16-byte records
  (NameOffset, NameLength, ValueOffset, ValueLength), lines 93-99.
- v4 namespace entry: proposal 28 bytes with data_len; pinned header has
  6 ULONGs (Flags, NameOffset, NameLength, AliasOffset, AliasLength,
  DataOffset) = 24 bytes, lines 143-151.
- v4 values: pinned header has a Flags+Count-headed array (lines 135-140) of
  20-byte records (lines 125-132); proposal read records without the array
  header.
- v6: the proposal's remembered 28/24/20/8 sizes and field order align with
  the pinned header (lines 164-201); that agreement is why v6 was kept, now
  cited field-by-field instead of remembered.

## Hash semantics (reverse-engineered, not proven)

Per review 0705's description of Geoff Chappell's documentation: rolling
32-bit hash starting at 0; for each character in the name prefix of
HashedLength bytes (up to the last hyphen), hash = hash * HashFactor +
lowercase(character); hash entries are sorted by Hash and carry indices into
the entry array. The parser recomputes with exact bounded ASCII-lowercase
names and REFUSES unsupported Unicode casing, validates HashedLength <=
NameLength and 2-byte alignment, index range/permutation/sorted order and
entry correspondence. These semantics are not claimed from fixture
agreement; they remain unproven until tested against real bytes.
