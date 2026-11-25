"""Emission of pe-closure/apiset-mapping@1 from a v6 NamespaceParse, with all
non-emittable records kept as rich evidence.

Emission is deliberately narrow. A contract is emitted only when ALL of:
  * no parse-level blocker finding exists (unknown header flag bits, or a
    hash table absent so hash/index/order validation was impossible);
  * its name matches the consumer's contract-name shape (api-ms-/ext-ms-,
    lowercase, consumer convention .dll suffix added here);
  * exactly ONE namespace entry carries that name: duplicate contract keys
    are REFUSED, agreeing duplicates included, never normalized away;
  * the entry's flags carry no bits outside the pinned header's named mask;
  * the entry has exactly one value record;
  * that value record's flags are zero (the pinned header names no v6 value
    flags, so any set bit is unknown semantics);
  * that value record has an empty context name (a default host);
  * the host string is non-empty and matches the consumer's bare-DLL-name
    shape, so the emitted document passes the consumer's own validator.

Multiple defaults, non-empty context records, empty hosts, unsupported
naming, unknown flag bits and duplicate keys stay explicitly unresolved and
are preserved as evidence records with exact derivation and provenance (raw
offsets and raw flags included). This module never flattens a
context-dependent selection into a guessed host: mapping@1 cannot encode
importer context.

Schema constants and regexes are aligned with the consumer validator source
(peinspect/apiset.py) supplied in the contract-0656 context tar; that source
is the definition of the pe-closure/apiset-mapping@1 shape used here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List

from . import LAYOUT_STATUS, PARSER_VERSION
from .namespace import (ENTRY_FLAG_EXTENSION, ENTRY_FLAG_SEALED,
                        ENTRY_FLAGS_KNOWN_MASK, VALUE_FLAGS_KNOWN_MASK,
                        NamespaceEntry, NamespaceParse)

SCHEMA = "pe-closure/apiset-mapping@1"

# Consumer contract-name shape, minus the ".dll" consumer convention: schema
# entry names carry no extension (e.g. "api-ms-win-core-file-l1-2-0").
CONTRACT_RE = re.compile(r"^(api-ms|ext-ms)-[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)
# Consumer host shape: a bare DLL file name.
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.dll$")


@dataclass
class EvidenceRecord:
    contract: str
    decision: str            # "unresolved" or "emitted"
    reason: str
    detail: dict


@dataclass
class MappingResult:
    mapping: Dict[str, str] = field(default_factory=dict)
    evidence: List[EvidenceRecord] = field(default_factory=list)

    def document(self) -> dict:
        """The pe-closure/apiset-mapping@1 document. Only 'schema' and
        'mappings' are populated; 'captured_on'/'source' belong to the writer
        that captures real runner bytes, not to this parser."""
        return {"schema": SCHEMA,
                "mappings": {name: {"host": host}
                             for name, host in sorted(self.mapping.items())}}

    def canonical_json(self) -> bytes:
        return json.dumps(self.document(), sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()

    def evidence_json(self) -> List[dict]:
        return [{"contract": e.contract, "decision": e.decision,
                 "reason": e.reason, "detail": e.detail} for e in self.evidence]


def _entry_base(entry: NamespaceEntry) -> dict:
    return {"name_offset": entry.name_offset, "name_length": entry.name_length,
            "hashed_len": entry.hashed_len, "entry_flags": entry.flags,
            "value_count": len(entry.values)}


def _values_detail(entry: NamespaceEntry) -> list:
    return [{"name": v.name, "value": v.value, "flags": v.flags,
             "value_offset": v.value_offset, "value_length": v.value_length}
            for v in entry.values]


def _entry_candidate(entry: NamespaceEntry) -> "tuple":
    """Return (candidate_host_or_None, reason_or_None, detail)."""
    base = _entry_base(entry)
    if entry.flags & ~ENTRY_FLAGS_KNOWN_MASK:
        return None, "unknown-entry-flag-bits", dict(
            base, note="entry flags %#x carry bits outside the pinned-header "
                       "mask %#x; unknown semantics, consumable mapping "
                       "refused" % (entry.flags, ENTRY_FLAGS_KNOWN_MASK))
    if entry.flags & ENTRY_FLAG_EXTENSION:
        return None, "entry-extension-flag", dict(
            base, values=_values_detail(entry),
            note="entry flags %#x carry EXTENSION (0x2); extension "
                 "redirection semantics are not independently established, "
                 "so no consumable mapping is emitted; raw evidence retained"
                 % entry.flags)
    if entry.flags & ENTRY_FLAG_SEALED:
        return None, "entry-sealed-unestablished", dict(
            base, values=_values_detail(entry),
            note="entry flags %#x carry SEALED (0x1); the pinned header "
                 "(line 89) names the bit without meaning and no source "
                 "establishes that it does not select a different context "
                 "host, so the entry is blocked; raw evidence retained"
                 % entry.flags)
    values = entry.values
    if not values:
        return None, "no-values", dict(base, note="entry declares zero value records")
    if len(values) > 1:
        return None, "multiple-values-context-dependent", dict(
            base, values=_values_detail(entry),
            note="more than one value record; per-importer/context selection "
                 "cannot be flattened into a single host")
    v = values[0]
    base["value_flags"] = v.flags
    base["value_offset"] = v.value_offset
    if v.flags & ~VALUE_FLAGS_KNOWN_MASK:
        return None, "unknown-value-flag-bits", dict(
            base, value_name=v.name, value=v.value,
            note="value flags %#x non-zero; the pinned header names no v6 "
                 "value flags, so any set bit is unknown semantics, "
                 "consumable mapping refused" % v.flags)
    if v.name:
        return None, "context-record", dict(
            base, value_name=v.name, value=v.value,
            note="single value carries a non-empty context name; semantics "
                 "not independently established")
    if not v.value:
        return None, "empty-host", dict(base, note="default host string is empty")
    if not HOST_RE.match(v.value) or "/" in v.value or "\\" in v.value or ".." in v.value:
        return None, "host-not-emittable", dict(
            base, value=v.value,
            note="host does not match the consumer's bare-DLL-name shape; "
                 "not emitted, kept as evidence")
    return v.value, None, base


def build_mapping(parse: NamespaceParse) -> MappingResult:
    """Build the optional mapping plus evidence from a parsed v6 namespace."""
    result = MappingResult()
    blockers = [f for f in parse.findings if f.get("blocks_mapping")]
    groups: Dict[str, list] = {}
    for entry in parse.entries:
        groups.setdefault(entry.name.casefold(), []).append(entry)

    for key in sorted(groups):
        entries = groups[key]
        display = entries[0].name
        if blockers:
            for f in blockers:
                result.evidence.append(EvidenceRecord(
                    display, "unresolved", f["kind"],
                    {"blocker": f["detail"],
                     "entries": [_entry_base(e) for e in entries],
                     "note": "parse-level refusal blocks every consumable "
                             "mapping from this namespace"}))
            continue
        if not CONTRACT_RE.match(display):
            result.evidence.append(EvidenceRecord(
                display, "unresolved", "contract-name-unsupported",
                {"occurrences": len(entries),
                 "note": "name is not an api-ms-/ext-ms- contract name; "
                         "unsupported alias/group naming stays unresolved"}))
            continue
        if len(entries) > 1:
            result.evidence.append(EvidenceRecord(
                display, "unresolved", "duplicate-contract-key",
                {"occurrences": len(entries),
                 "entries": [dict(_entry_base(e), values=_values_detail(e))
                             for e in entries],
                 "note": "duplicate contract key refused, agreeing duplicates "
                         "included; never normalized into one emission"}))
            continue
        entry = entries[0]
        cand, reason, detail = _entry_candidate(entry)
        if reason:
            result.evidence.append(EvidenceRecord(display, "unresolved", reason, detail))
            continue
        result.evidence.append(EvidenceRecord(
            display, "emitted", "single-default-host",
            dict(detail, host=cand, layout_status=LAYOUT_STATUS)))
        result.mapping[display.lower() + ".dll"] = cand
    return result


def full_report(parse: NamespaceParse, result: MappingResult,
                source_path: str = None, os_build: str = None,
                image_sha256: str = None) -> dict:
    """Provenance-preserving report object for a capture run. The writer
    supplies source_path, os_build and image_sha256 from the actual runner;
    they are never invented here."""
    return {
        "parser": PARSER_VERSION,
        "layout_status": LAYOUT_STATUS,
        "namespace_version": parse.version,
        "namespace_section_sha256": parse.section_sha256,
        "namespace_section_size": parse.section_size,
        "namespace_parsed_size": parse.namespace_size,
        "namespace_parsed_sha256": parse.namespace_sha256,
        "namespace_header_flags": parse.header_flags,
        "namespace_hash_factor": parse.hash_factor,
        "mapping_blockers": [f["kind"] for f in parse.findings
                             if f.get("blocks_mapping")],
        "image_sha256": image_sha256,
        "source_path": source_path,
        "os_build": os_build,
        "mapping_schema": SCHEMA,
        "mapping_canonical_sha256": result.canonical_sha256(),
        "emitted_count": len(result.mapping),
        "unresolved_count": sum(1 for e in result.evidence if e.decision == "unresolved"),
        "parse_findings": parse.findings,
        "evidence": result.evidence_json(),
    }
