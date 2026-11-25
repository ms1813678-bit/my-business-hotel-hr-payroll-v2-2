# Findings: pe-closure-0627 repair build (25 November 2026)

## Withdrawal of the earlier delivery
The earlier deliverable (sha256 622cd4c4...) must not be relied on. Its parser read PE32
optional-header offsets (NumberOfRvaAndSizes at 92, directories at 96) while requiring
PE32+. For PE32+ the spec puts NumberOfRvaAndSizes at 108 and the data directories at 112
(optional header 112 + 16*8 = 240 bytes). Its fixtures used the same wrong layout, so its
40 passing tests did not qualify the parser. A PE32+ image with HeapReserve high dword 0
at offset 92 parsed to zero directories and an empty import list, without error. The
claim was checked against the spec layout and against our code and is confirmed.

## What was repaired (each external claim checked in code)
- Offsets: 108/112; PE32 (0x10b) refused; NumberOfRvaAndSizes > 16 or beyond
  SizeOfOptionalHeader refused.
- RVA reads are span-aware: start AND length must sit in one mapped region (headers or one
  section's virtual extent). Bytes in VirtualSize beyond raw data read as zero. Confirmed
  the old code checked only the start (plus whole-file bounds).
- Strings are bounded by the containing region, MAX_STRING, and the directory extent for
  forwarders. Thunk arrays are bounded by the starting region. Export tables must fit one
  region.
- Descriptor iteration is bounded by the declared directory size and must reach a zero
  terminator inside it. A declared size too small or zero (with an RVA) is refused, never
  an empty success. This is stricter than the Windows loader, which may tolerate wrong
  directory sizes; real files with an under-declared size would be refused.
- Section checks: virtual overlap, raw overlap, raw range in file, raw data or virtual range
  inside headers, section table inside SizeOfHeaders, end within SizeOfImage.
- Size caps are applied from fstat before content is read (PE files, mapping file).
  Cap default 64 MiB for a PE (was 512 MiB, read first).
- Indexing: non-recursive by default, name-only (no hashing), bounded by file count and
  depth; skipped/unreadable entries are listed in `index_notes`. Candidates are read and
  hashed lazily, only for requested names, under per-file and total byte budgets.
  Over-cap or unreadable candidates become `candidate-unreadable` edges.
- Mapping is validated inside `run_closure` (public API). `resolved-via-supplied-mapping`
  states it is not native resolution and the mapping provenance is reported as unverified.
- Forwarders: labelled `over_approximation`, split at the last dot, module parts with a dot
  reported as `unparseable-forwarder`, not truncated.
- Manifest: SHA256SUMS.txt has no self-entry, two-space format, covers actual members.

## Evidence classes (precise)
- SUPPLIED: tests/data/static-imports.json (hash-checked in the test) with the DLL-name lists
  for python.exe, python314.dll, vcruntime140.dll, vcruntime140_1.dll.
- QUOTED, UNVERIFIED: the external report's values (python.exe import directory RVA 10444,
  size 180 at optional offset 120; python314.dll 5950600/400). One test only checks that
  such a value lands at offset 120 in our synthetic header. No bytes were supplied.
- SYNTHETIC: every PE in the tests is built from the spec layout by tests/spec_pe.py. The
  regression test builds stand-ins carrying exactly the supplied import-name lists
  (symbol names invented) and checks the parser and closure reproduce them.
- NOT DONE: no real python.exe or python314.dll bytes were supplied, so no real-runtime
  binary regression was run. Nothing was downloaded. The external claim that the pinned
  python.exe parses to zero imports with the old code could not be reproduced by us.

## What ran vs not
Ran (Linux sandbox, stdlib only, no network): `python3 -m unittest discover` on the repaired
source, 79 tests, all pass. New parser tests were also run against the old parser source:
the supplied-list regression fails there (imports dropped), and test_pe cannot even import
(API changed), which is consistent with the bug. Mutation fuzz (3000 seeded byte edits) and
every-length truncation raise only PEFormatError.
Not run: any Windows execution, any real PE, loader snaps/ETW, restricted-token probes,
API-set capture, any test on the runner. The spec layout was applied from knowledge of the
Microsoft PE format document; it was not fetched (no network).

## What the inspector can and cannot prove
Can: static import, delay-import and forwarder names of a PE32+ image under strict bounds;
presence, hash and machine of same-name candidates in declared snapshot roots; collisions,
architecture mismatches, unresolved API-set names.
Cannot: the cause of C0000022, restricted-token readability, loader policy or selected
file, successful load, network denial. Parse success and file presence prove none of these.
Forwarder edges over-approximate. A fully resolved static closure fits both a healthy and
a failing launch.

## Open items
1. Real-binary regression: needs the actual pinned python.exe/python314.dll bytes supplied
   (hash-checked) to confirm non-empty import parse equals the supplied JSON, including the
   quoted directory values.
2. Writer's further findings are pending; not covered here.
3. Directory-size strictness may refuse real images with sloppy sizes; decide whether to
   relax with evidence from real files.
4. Not covered: bound imports, TLS callbacks, SxS manifests/activation contexts, KnownDLLs,
   load-time hooks, resource-only or .NET dependencies, ARM64X.
5. Native captures still needed on the runner (failing-run loader diagnostics, token
   readability probe, API-set mapping in the pe-closure/apiset-mapping@1 schema).
