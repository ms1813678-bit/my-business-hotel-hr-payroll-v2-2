"""Read-only, standard-library-only parser for the API-set schema namespace
section (.apiset) of apisetschema.dll, repaired to namespace version 6 only.

LAYOUT STATUS: SOURCE_DERIVED_UNTESTED. The v6 struct layout is derived
field-by-field from a pinned, hash-verified copy of the phnt header
ntpebteb.h at revision 53fbbdc5b5d2b08761db1c7b26bfa8c820924356
(SHA256 9869d45323706a6bf7cb3ed168b4ca6560aef194f1c6a4d37aaad74a356ad248).
Those definitions are reverse-engineered and private: they are NOT a
Microsoft ABI guarantee, and the layout has never been tested against real
apisetschema.dll bytes. Hash semantics come from the 0705 review's
description of Geoff Chappell's reverse-engineered documentation, likewise
not normative and not fetched here (no network). Fixture agreement proves
parser/fixture consistency only, never real-byte semantics.

Versions 2 and 4 are REFUSED: the previously remembered v2/v4 layouts were
wrong against the pinned header (see docs/LAYOUT-V6.md) and were removed
rather than corrected without source-backed tests. Every other version is
refused as before. Never guessed.

Never loads, executes, or resolves any DLL. Accepts bytes or reads a file in
binary mode with a pre-read size cap.
"""

PARSER_VERSION = "apiset-namespace-parser/0.3.0-v6-size-bound-extension-block"
LAYOUT_STATUS = "SOURCE_DERIVED_UNTESTED"
