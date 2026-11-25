# Sources and provenance, repaired deliverable

## Pinned layout source (now available, hash-verified)

1. phnt ntpebteb.h, revision 53fbbdc5b5d2b08761db1c7b26bfa8c820924356
   - URL: https://raw.githubusercontent.com/winsiderss/phnt/53fbbdc5b5d2b08761db1c7b26bfa8c820924356/ntpebteb.h
   - SHA256 9869d45323706a6bf7cb3ed168b4ca6560aef194f1c6a4d37aaad74a356ad248
     (verified locally against the pinned copy supplied with review 0705).
   - DOES document (as reverse-engineered, private definitions): v2/v4/v6
     namespace structs, flag names, section name ".apiset".
   - IS NOT: a Microsoft ABI guarantee. System Informer/phnt definitions are
     community reverse engineering. Read as text only; never compiled or
     executed here.

2. https://www.geoffchappell.com/studies/windows/win32/apisetschema/index.htm
   - Independent reverse-engineered layout and semantics; the source of the
     hash semantics (rolling u32 from 0, multiply by the header HashFactor,
     add the lowercased character, over the name prefix of HashedLength
     bytes; sorted hash entries holding indices into the entry array) AS
     DESCRIBED BY review 0705. The page itself was not fetched (no network);
     the description is unverified external content and is treated as a
     lead, not proof.

## Other cited sources

3. https://learn.microsoft.com/en-us/windows/win32/apiindex/windows-apisets
   - Documents contract-vs-host semantics only; no binary offsets.
4. https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
   - Microsoft PE/COFF spec; backs apisetschema/pesections.py only.
5. https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45549153866
   - Requester-reported native capture run. Not fetched (no network);
     external, unverified context.

## Provenance correction (preserved from review thread)

The first 0705 review (SHA256 7b23d903f5a57fbf8b22671ea88af593ab48f641719da71a66644907a09d3d2a)
named "original archive SHA256 4c462f60e50c018a50cf689cecb5250bc1fee5c69a695c503ebec603b1de7e5a".
That value was a transfer-filename suffix, not a byte hash. The corrected
review (SHA256 6f348ea69f3ca025afd17433ec2627401b64a222db5923d7f098c7a4ef5cdb01)
gives the true byte SHA256 d9e75ceabfea3ee7eb82826017df8c5d1d7128805d8dc26e8b7fdeb5b448c4e0,
which matches the archive received here and was verified locally. Technical
requirements were unchanged between the two reviews (diff: provenance line
only).

## Input hash verification (this repair)

- apiset-capture-0656.tar.gz  d9e75cea...c4e0  MATCHES parent-stated hash
- review (first)              7b23d903...3d2a  MATCHES parent-stated hash
- review (corrected)          6f348ea6...db01  MATCHES parent-stated hash
- pinned phnt ntpebteb.h      9869d453...d248  MATCHES parent-stated hash
- REPORT-0656.json            1db89c52...2c55  no hash was stated; recorded for provenance

## Remaining source gap

The v6 layout is now source-derived, but no real apisetschema.dll bytes have
been parsed: actual-byte qualification remains with the native writer, who
must capture the exact build-20348 apisetschema.dll (image SHA256, section
SHA256, header version) and run this parser against it before any emitted
mapping is treated as verified.
