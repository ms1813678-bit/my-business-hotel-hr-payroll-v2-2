from calendar import monthrange
from datetime import date
from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.legacy_department_evidence import MONTHS, parse_legacy_department


def sample():
    wb = Workbook(); ws = wb.active; ws.title = 'Employee_List'
    ws.append(['Synthetic']); ws.append(['ID', 'Name', 'Hiring Date', 'End Work'])
    ws.append([1, 'Synthetic employee', date(2026, 1, 2), '30/6/2026'])
    for month, name in enumerate(MONTHS, 1):
        ws = wb.create_sheet(name); ws.append([f'ATTENDANCE {name} 2026'])
        ws.append(['ID', 'Name', *range(1, monthrange(2026, month)[1]+1)])
        ws.append([1, 'Synthetic employee', 'w', None, 'u'])
        ws.append(['TOTAL'])
    out = BytesIO(); wb.save(out); wb.close(); return out.getvalue()


def test_original_bytes_coordinates_blanks_and_codes_retained():
    payload = sample(); result = parse_legacy_department(payload, source_identity='synthetic:message', year=2026)
    assert result.original_bytes == payload and result.source_sha256 == sha256(payload).hexdigest()
    assert result.employees[0]['end_work_date'] == date(2026, 6, 30)
    assert result.days[0]['raw_value'] == 'w' and result.days[0]['code'] == 'W'
    assert result.days[0]['cell'] == 'C3'
    assert result.days[1]['code'] is None and result.days[1]['raw_value'] is None
    assert any(issue['code'] == 'day_code_outside_service' for issue in result.issues)
    assert len(result.days) == 365


def test_corrupt_container_refuses():
    with pytest.raises(ValueError): parse_legacy_department(b'badzip', source_identity='synthetic', year=2026)
