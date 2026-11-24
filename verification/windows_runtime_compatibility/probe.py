"""Benign compatibility probe, NOT sandbox acceptance or a Windows gate bypass."""
import argparse
import base64
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--source', required=True)
    args = p.parse_args()
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    from app.fingerprint_evidence import parse_fingerprint_evidence
    from app.fingerprint_bounded_parse import _decode_worker
    source = Path(args.source)
    payload = source.read_bytes()
    result = parse_fingerprint_evidence(payload, filename=source.name, verified_aliases={'SYN': 1})
    assert len(result.occurrences) == 4
    assert result.occurrences[0]['employee_id'] is None
    assert result.occurrences[1]['punch_at'] == result.occurrences[2]['punch_at']
    assert result.occurrences[3]['punch_at'] == datetime(2026, 11, 2, 7)
    output = dict(status='ok', period_start=result.period_start, period_end=result.period_end,
                  occurrences=[dict(v) for v in result.occurrences], issues=[dict(v) for v in result.issues])
    def encode(value):
        if type(value) in (date, datetime):
            return value.isoformat()
        raise TypeError(type(value).__name__)
    wire = json.dumps(output, default=encode, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode()
    first, last, events, issues = _decode_worker(wire, {'SYN': 1})
    assert len(events) == 4 and first == date(2026, 11, 1) and last == date(2026, 11, 2)
    modules = {name: getattr(module, '__file__', '<built-in>') for name, module in sorted(sys.modules.items())
               if module is not None}
    import importlib.metadata
    versions = {}
    for name in ('openpyxl', 'xlrd', 'et_xmlfile', 'defusedxml', 'lxml'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    sources = {}
    for name in ('fingerprint_parse_worker', 'fingerprint_bounded_parse', 'fingerprint_evidence', 'attendance_import'):
        path = root / 'app' / (name + '.py')
        sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    print(json.dumps(dict(kind='benign-compatibility-only', platform=sys.platform,
                         python=sys.version, executable=sys.executable, source_sha256=hashlib.sha256(payload).hexdigest(),
                         source_suffix=source.suffix, source_hashes=sources, occurrences=len(events),
                         output_bytes=len(wire), duplicate_preserved=True, versions=versions,
                         sensitive_imports={name: name in modules for name in ('socket', '_socket', '_ssl', 'ssl', '_ctypes',
                                          'ctypes', '_overlapped', 'asyncio', 'http.client', 'urllib.request')},
                         modules=modules), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
