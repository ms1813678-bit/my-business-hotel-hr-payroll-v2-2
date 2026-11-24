"""Source-bound fingerprint occurrences, never a shift/direction or DB import.

New department reconciliation must use this evidence adapter rather than the
legacy importer's deduplicated event list. Original bytes and every occurrence
are retained, including duplicate timestamps. Alias bindings are explicit caller
inputs, not guessed from name/case normalization by this adapter.
"""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from zipfile import BadZipFile, ZipFile
import zlib
from xml.etree.ElementTree import ParseError

from app.attendance_import import parse_attendance_workbook


@dataclass(frozen=True)
class FingerprintEvidence:
    source_sha256: str
    original_bytes: bytes
    period_start: date | None
    period_end: date | None
    occurrences: tuple[dict, ...]
    issues: tuple[dict, ...]


def parse_fingerprint_evidence(payload: bytes, *, filename: str, verified_aliases: dict):
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= 10_000_000:
        raise ValueError('Bounded original fingerprint bytes required.')
    if type(verified_aliases) is not dict or not verified_aliases:
        raise ValueError('Explicit verified alias bindings required.')
    for key, employee_id in verified_aliases.items():
        if (not isinstance(key, str) or not key.strip() or type(employee_id) is not int or employee_id<1):
            raise ValueError('Explicit verified alias and employee ID required.')
    if Path(filename).suffix.lower() == '.xlsx':
        try:
            with ZipFile(BytesIO(payload)) as archive:
                members=archive.infolist()
                names=[m.filename for m in members]
                if (len(members)>3000 or sum(m.file_size for m in members)>60_000_000
                        or len(names)!=len(set(names))
                        or any('vbaproject' in n.lower() for n in names)):
                    raise ValueError('Fingerprint container exceeds safe evidence bounds.')
                if not {'[Content_Types].xml','xl/workbook.xml'}.issubset(names):
                    raise ValueError('Required fingerprint workbook parts missing.')
        except BadZipFile as exc:
            raise ValueError('Invalid fingerprint workbook container.') from exc
    # Legacy normalization collisions are refused by importer, not firstwins.
    from openpyxl.utils.exceptions import InvalidFileException
    try:
        import xlrd
        from xlrd.compdoc import CompDocError
        legacy_errors=(xlrd.XLRDError,CompDocError)
    except ImportError:
        legacy_errors=()
    try:
        result = parse_attendance_workbook(payload, filename, 'fingerprint', verified_aliases,
            preserve_duplicate_punches=True)
    except (BadZipFile, ParseError, InvalidFileException, KeyError, zlib.error, *legacy_errors) as exc:
        raise ValueError('Invalid original fingerprint workbook.') from exc
    return FingerprintEvidence(sha256(payload).hexdigest(), payload, result['period_start'],
        result['period_end'], tuple(MappingProxyType(dict(event)) for event in result['events']),
        tuple(MappingProxyType(dict(issue)) for issue in result['issues']))
