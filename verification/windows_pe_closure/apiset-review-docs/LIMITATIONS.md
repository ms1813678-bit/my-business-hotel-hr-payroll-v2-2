# Limitations and findings, repaired apiset-capture-0656 deliverable

## Scope actually delivered

- apisetschema/pesections.py: unchanged, strict PE32/PE32+ section extraction
  (PE/COFF-spec-backed). Bounded reads, truncation and overlap refusal.
- apisetschema/namespace.py: v6 ONLY. Layout derived field-by-field from the
  pinned, hash-verified phnt header (docs/LAYOUT-V6.md cites every line).
  Versions 2, 4 and all others raise UnsupportedVersionError; the remembered
  v2/v4 layouts were wrong against the pinned header and were removed.
  Validates offsets, count multiplications, all extents, UTF-16 alignment
  and strict decode, truncation, HashedLength <= NameLength and evenness,
  and the full hash table: sorted order, index range, index permutation, and
  entry correspondence by recomputation (review-described rolling u32 hash,
  ASCII-only lowercase, explicit refusal of unsupported Unicode casing).
  Overlap policy: structural regions tolerate no overlap; strings tolerate
  only validated exact immutable reuse (identical extent + identical decoded
  text, recorded as a finding); partial string overlap is a format error.
- apisetschema/mapping.py: emission of optional pe-closure/apiset-mapping@1
  containing ONLY unambiguous default single hosts. Duplicate contract keys
  are refused, agreeing duplicates included, never normalized. Unknown flag
  bits refuse consumable mapping (header-wide for header flags, per-entry
  for entry/value flags; the pinned header names no v6 value flags, so any
  non-zero value flags refuse). Context/multi-value/empty-host/unsupported-
  naming records stay unresolved as rich evidence with raw offsets and raw
  flags. A parse-level blocker (unknown header flag bits, absent hash table)
  empties the consumable mapping while preserving all evidence.
- apisetschema/cli.py: read-only driver; exit 0 parsed / 2 format error /
  3 unsupported version / 4 usage.
- tests/: 68 stdlib unittest tests against synthetic fixtures built
  independently from the pinned header (tests/fixtures.py shares no
  constants with the parser).

## Hard limits honored

- Standard library only; no network, no pip, no downloads.
- The pinned header was read as text only; nothing was compiled or executed
  from any input. No DLL loaded or executed; no LoadLibrary, no PEB walking,
  no memory scraping, no fallback map.
- No certificates or CA material involved. No loader cause, C0000022,
  restricted-readability or network-denial claim. No loader-resolution
  claim: an emitted mapping is a candidate from static bytes, not the
  loader's actual selection.

## What ran vs what did not

- RAN: 68/68 unit tests pass (Linux sandbox, CPython 3.10, stdlib unittest).
  CLI smoke test on a synthetic PE fixture: v6 parse with correct emission
  and evidence retention, and exit 3 on a version-4-patched section.
- DID NOT RUN: any parse of a real apisetschema.dll; anything on a Windows
  runner; any validation against real runtime bytes. No real apisetschema.dll
  exists in this sandbox. Synthetic vs source-backed labeling: fixture bytes
  are synthetic; struct layouts and flag masks are source-backed (pinned
  header); hash semantics are review-described reverse engineering.

## Known gaps

1. SOURCE_DERIVED_UNTESTED: the v6 layout is source-backed but never tested
   against real bytes. The native writer must capture the exact build-20348
   apisetschema.dll (image SHA256, section SHA256, version) and run this
   parser before any emitted mapping is used.
2. Hash semantics are as described by the review of Chappell's page (itself
   unfetched, no network); a real-bytes mismatch raises loudly rather than
   mis-parsing, which is the intended tripwire.
3. Behavioral meaning of the named flags (SEALED, HOST_EXTENSION,
   EXTENSION) is not asserted beyond the header's names; raw values are
   preserved in findings/evidence.
4. Absent hash table (HashOffset == 0 with entries) blocks the consumable
   mapping by policy; if real bytes ever legitimately omit the table, that
   is a one-line policy change in build_mapping after source evidence.
5. The ".dll" suffix on emitted keys is the consumer's convention, not
   physical-file proof; matching is case-insensitive per the brief.

## 0714 repair addendum (namespace Size bound, extension flags)

Changes (namespace.py 0.3.0, mapping.py):
1. The header Size DWORD is validated (>= 28, <= section length) and every
   read and region is bounded by it; reads go through data[:Size]. Trailing
   bytes are never addressable (a reference into them is a format error),
   are recorded in a finding (length, all-zero flag, sha256) and accepted
   only outside all referenced bytes. Provenance keeps the full-section
   hash/size and the parsed-namespace hash/size separately
   (section_sha256/namespace_sha256; report keys namespace_section_* and
   namespace_parsed_*).
2. Namespace HOST_EXTENSION (0x2, also 0x3) blocks all consumable mapping;
   entry EXTENSION (0x2, also 0x3) blocks that entry. Raw flags and values
   stay in findings/evidence. Unknown bits still block.
3. SEALED (0x1): the pinned header (lines 85 and 89) names the bits and
   gives no meaning. No fetched source (Chappell page was NOT fetched, no
   network) states that SEALED does not select a different context host.
   Therefore SEALED entries are BLOCKED (entry-sealed-unestablished) and a
   namespace SEALED header blocks all mapping. Consequence on the captured
   build-20348 bytes: 925 of 936 entries carry SEALED, so only 2 entries
   emit (flags 0, single default host); 925 are unresolved with evidence.
   If SEALED meaning is later established from a source, relaxing it is a
   small change in mapping._entry_candidate.

What ran: stdlib unittest, CPython (Linux sandbox), 95 tests, with and
without the captured section JSON, plus a read-only analysis script that
base64-decoded the captured bytes and parsed them in memory. Nothing from
the attachments was compiled or executed; no DLL loaded; no network.
Not done: no loader/PEB/memory work, no claim about loader behavior, no
new namespace versions, no mapping use or flattening. A successful parse is
not loader or security proof.
