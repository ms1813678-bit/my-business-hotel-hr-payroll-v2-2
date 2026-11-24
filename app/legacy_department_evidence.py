"""Read-only legacy department evidence. No DB matching or balance authority.

Returns original bytes for private preservation, hashes, exact raw cells and
quality issues. Caller must preserve the returned bundle before presenting a
review. Does not evaluate formulas, follow links or import historical records.
"""
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


@dataclass(frozen=True)
class LegacyDepartmentEvidence:
    source_identity: str
    source_sha256: str
    original_bytes: bytes
    employees: tuple[dict, ...]
    days: tuple[dict, ...]
    issues: tuple[dict, ...]


def parse_legacy_department(payload: bytes, *, source_identity: str, year: int) -> LegacyDepartmentEvidence:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= 10_000_000:
        raise ValueError('Bounded original source bytes required.')
    if not isinstance(source_identity, str) or not source_identity.strip() or len(source_identity) > 1000:
        raise ValueError('Explicit bounded source identity required.')
    if type(year) is not int or not 1900 <= year <= 9998:
        raise ValueError('Explicit source year required.')
    try:
        with ZipFile(BytesIO(payload)) as container:
            members = container.infolist()
            if len(members) > 3000 or sum(m.file_size for m in members) > 60_000_000:
                raise ValueError('Source workbook container exceeds bound.')
            names = [m.filename for m in members]
            if len(set(names)) != len(names) or any('vbaproject' in n.lower() for n in names):
                raise ValueError('Ambiguous or macro-bearing source workbook refused.')
        wb = load_workbook(BytesIO(payload), read_only=True, data_only=False, keep_links=False)
    except BadZipFile as exc:
        raise ValueError('Invalid source workbook container.') from exc
    employees = []; days = []; issues = []
    def issue(code, sheet, cell):
        issues.append({'code': code, 'sheet': sheet, 'cell': cell})
    def raw(cell):
        value = cell.value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value
    def identity(value):
        if type(value) is int and value > 0:
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    def parsed_date(cell):
        value = cell.value
        if isinstance(value, datetime): return value.date()
        if type(value) is date: return value
        if isinstance(value, str) and re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', value.strip()):
            try:
                d, m, y = map(int, value.strip().split('/'))
                return date(y, m, d)
            except ValueError:
                pass
        return None
    try:
        if 'Employee_List' not in wb.sheetnames:
            raise ValueError('Employee_List source sheet required.')
        for ws in wb:
            if ws.max_row > 2000 or ws.max_column > 60:
                raise ValueError('Legacy source dimensions exceed bound.')
        ws = wb['Employee_List']
        headers = {str(c.value).strip(): c.column for c in ws[2] if c.value is not None}
        if any(h not in headers for h in ('ID', 'Name', 'Hiring Date')):
            raise ValueError('Recognized employee source headers required.')
        seen = {}; names = {}
        for row in range(3, ws.max_row + 1):
            id_cell = ws.cell(row, headers['ID']); name_cell = ws.cell(row, headers['Name'])
            if id_cell.value is None and name_cell.value is None: continue
            code = identity(id_cell.value)
            name = name_cell.value.strip() if isinstance(name_cell.value, str) else None
            hire_cell = ws.cell(row, headers['Hiring Date'])
            hire = parsed_date(hire_cell)
            end_cell = ws.cell(row, headers['End Work']) if 'End Work' in headers else None
            end = parsed_date(end_cell) if end_cell else None
            if code is None: issue('missing_employee_code', ws.title, id_cell.coordinate)
            elif code in seen: issue('duplicate_employee_code', ws.title, id_cell.coordinate)
            if name is None: issue('missing_employee_name', ws.title, name_cell.coordinate)
            elif name in names: issue('duplicate_employee_name', ws.title, name_cell.coordinate)
            if hire is None: issue('missing_or_invalid_hire_date', ws.title, hire_cell.coordinate)
            if end_cell and end_cell.value is not None and end is None:
                issue('invalid_end_work_date', ws.title, end_cell.coordinate)
            if hire and end and end < hire: issue('end_before_hire', ws.title, end_cell.coordinate)
            entry = {'source_code': code, 'source_name': name, 'hire_date': hire, 'end_work_date': end,
                     'sheet': ws.title, 'row': row, 'raw_id': raw(id_cell), 'raw_name': raw(name_cell),
                     'raw_hire_date': raw(hire_cell), 'raw_end_work_date': raw(end_cell) if end_cell else None}
            employees.append(entry)
            if code is not None: seen.setdefault(code, entry)
            if name is not None: names[name] = row
        for month, sheet in enumerate(MONTHS, 1):
            if sheet not in wb.sheetnames:
                issue('missing_month_sheet', sheet, 'A1'); continue
            ws = wb[sheet]
            title = str(ws.cell(1, 1).value or '')
            if str(year) not in title: issue('source_year_not_confirmed_by_title', sheet, 'A1')
            n = monthrange(year, month)[1]
            if ws.cell(2, 1).value != 'ID' or ws.cell(2, 2).value != 'Name' or any(ws.cell(2, d+2).value != d for d in range(1, n+1)):
                raise ValueError('Recognized explicit month day headers required.')
            monthly_ids = set()
            for row in range(3, ws.max_row+1):
                id_cell = ws.cell(row, 1); name_cell = ws.cell(row, 2)
                if str(id_cell.value).strip().upper() == 'TOTAL': break
                if id_cell.value is None and name_cell.value is None: continue
                code = identity(id_cell.value)
                if code is None: issue('missing_month_employee_code', sheet, id_cell.coordinate); continue
                if code in monthly_ids: issue('duplicate_month_employee_code', sheet, id_cell.coordinate)
                monthly_ids.add(code)
                employee = seen.get(code)
                if employee is None: issue('month_identity_not_in_employee_list', sheet, id_cell.coordinate)
                elif name_cell.value != employee['source_name']: issue('month_employee_name_mismatch', sheet, name_cell.coordinate)
                for d in range(1, n+1):
                    cell = ws.cell(row, d+2); value = cell.value; day = date(year, month, d)
                    coordinate = f'{get_column_letter(d+2)}{row}'
                    normalized = value.strip().upper() if isinstance(value, str) else value
                    recognized = normalized if normalized in ('P', 'A', 'S', 'U', 'H', 'W', '—') else None
                    if cell.data_type == 'f': issue('formula_in_day_cell', sheet, coordinate); recognized = None
                    elif value is not None and recognized is None and normalized != '': issue('unknown_day_code', sheet, coordinate)
                    # Blanks remain unknown, not presence or absence.
                    if employee and employee['hire_date']:
                        outside = day < employee['hire_date'] or (employee['end_work_date'] and day > employee['end_work_date'])
                        if outside and recognized not in (None, '—'): issue('day_code_outside_service', sheet, coordinate)
                        if not outside and recognized == '—': issue('outside_marker_during_service', sheet, coordinate)
                    days.append({'source_code': code, 'work_date': day, 'raw_value': raw(cell),
                                 'code': recognized, 'sheet': sheet, 'cell': coordinate})
        return LegacyDepartmentEvidence(source_identity, sha256(payload).hexdigest(), payload,
                                        tuple(employees), tuple(days), tuple(issues))
    finally:
        wb.close()
