"""Local candidate entry, never wired to product or used as acceptance proof.
The native candidate must establish LPAC/Job before resuming this interpreter.
"""
import base64
from datetime import date, datetime
import json
import os
import sys


def main():
    if os.name != 'nt':
        raise RuntimeError('Windows candidate only')
    from app.fingerprint_evidence import parse_fingerprint_evidence
    raw = sys.stdin.buffer.read(16_000_001)
    if len(raw) > 16_000_000:
        raise ValueError('Bounded input required')
    values = json.loads(raw)
    if type(values) is not dict or set(values) != {'payload', 'filename', 'aliases'}:
        raise ValueError('Exact input schema required')
    payload = base64.b64decode(values['payload'], validate=True)
    try:
        result = parse_fingerprint_evidence(payload, filename=values['filename'], verified_aliases=values['aliases'])
        output = dict(status='ok', period_start=result.period_start, period_end=result.period_end,
                      occurrences=[dict(v) for v in result.occurrences], issues=[dict(v) for v in result.issues])
    except ValueError:
        output = dict(status='refused')
    def encode(value):
        if type(value) in (date, datetime):
            return value.isoformat()
        raise TypeError('Unsupported output')
    encoded = json.dumps(output, default=encode, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(encoded) > 4_000_000:
        raise ValueError('Bounded output required')
    sys.stdout.buffer.write(encoded)


if __name__ == '__main__':
    main()
