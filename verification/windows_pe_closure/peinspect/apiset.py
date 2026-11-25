"""API-set mapping input: schema definition and strict validation.

API-set contract names (api-ms-*, ext-ms-*) are NEVER resolved by guessing.
They stay explicitly unresolved unless the caller supplies a mapping file
captured separately on the target runner system. This module defines and
validates that mapping input. It performs no resolution itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Dict

SCHEMA = "pe-closure/apiset-mapping@1"

NAME_RE = re.compile(r"^(api-ms|ext-ms)-[a-z0-9][a-z0-9._-]*\.dll$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.dll$")

SCHEMA_DOC = {
    "schema": SCHEMA,
    "mappings": {
        "api-ms-win-example-l1-1-0.dll": {"host": "examplebase.dll"}
    },
    "notes": [
        "Keys: lowercase API-set contract names ending in .dll.",
        "host: a bare DLL file name (no path, no separators) as observed on the runner.",
        "Optional top-level 'captured_on' (string) and 'source' (string) record provenance.",
        "Capture belongs to the writer on the target runner; this tool only validates shape.",
    ],
}


class MappingValidationError(ValueError):
    pass


def validate(obj) -> Dict[str, str]:
    """Validate a decoded JSON mapping object; return {contract_name: host}."""
    if not isinstance(obj, dict):
        raise MappingValidationError("mapping root must be a JSON object")
    if obj.get("schema") != SCHEMA:
        raise MappingValidationError("mapping schema must be %r, got %r"
                                     % (SCHEMA, obj.get("schema")))
    allowed = {"schema", "mappings", "captured_on", "source"}
    extra = set(obj) - allowed
    if extra:
        raise MappingValidationError("unexpected mapping keys: %s" % sorted(extra))
    for opt in ("captured_on", "source"):
        if opt in obj and not isinstance(obj[opt], str):
            raise MappingValidationError("%r must be a string" % opt)
    mappings = obj.get("mappings")
    if not isinstance(mappings, dict):
        raise MappingValidationError("'mappings' must be an object")
    out: Dict[str, str] = {}
    for name, entry in mappings.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise MappingValidationError(
                "invalid API-set contract name %r: must match %s" % (name, NAME_RE.pattern))
        if not isinstance(entry, dict) or set(entry) != {"host"}:
            raise MappingValidationError(
                "mapping for %r must be an object with exactly one 'host' key" % name)
        host = entry["host"]
        if not isinstance(host, str) or not HOST_RE.match(host):
            raise MappingValidationError(
                "invalid host %r for %r: bare DLL file name only" % (host, name))
        if "/" in host or "\\" in host or ".." in host:
            raise MappingValidationError("host %r must not contain path components" % host)
        out[name] = host
    return out


MAX_MAPPING_BYTES = 4 * 1024 * 1024


def load_mapping(path: str):
    """Load and validate a mapping file. Returns (mapping_dict, sha256_hex).

    The size cap is enforced from fstat BEFORE content is read.
    """
    import os
    import stat
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MappingValidationError("mapping path is not a regular file")
        if st.st_size > MAX_MAPPING_BYTES:
            raise MappingValidationError("mapping file exceeds %d byte bound" % MAX_MAPPING_BYTES)
        raw = b""
        while len(raw) <= MAX_MAPPING_BYTES:
            chunk = os.read(fd, MAX_MAPPING_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    if len(raw) > MAX_MAPPING_BYTES:
        raise MappingValidationError("mapping file exceeds %d byte bound" % MAX_MAPPING_BYTES)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MappingValidationError("mapping file is not valid UTF-8 JSON: %s" % exc)
    return validate(obj), hashlib.sha256(raw).hexdigest()


def validate_document(obj):
    """Public-API entry: validate a decoded mapping document, return
    (mapping_dict, sha256_of_canonical_json)."""
    mapping = validate(obj)
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return mapping, hashlib.sha256(canon).hexdigest()
