"""Isolated two-tab department workbook exchange, not registered on any route.

Caller supplies resolved identities, employment dates and authoritative balances.
This module does not accrue, choose policies, read accounts, post or match punches.
Returned formulas are never executed and workbook metadata never grants authority.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image
from openpyxl.styles import Alignment, Border, Font, PatternFill, Protection, Side
from openpyxl.worksheet.datavalidation import DataValidation

CODES = ('P', 'A', 'S', 'U', 'H', 'W')
BALANCE_SHEET = 'الأرصدة'
ENTRY_SHEET = 'التسجيل اليومي'
FIRST_EMPLOYEE_ROW = 7
NAVY, GOLD, CREAM = '1A2947', 'B5955A', 'F5F1E8'


@dataclass(frozen=True)
class BalanceSnapshot:
    carry: Decimal
    entitled_through_month_end: Decimal
    used_before_month: Decimal


@dataclass(frozen=True)
class DepartmentWorkbookEmployee:
    employee_id: int
    code: str
    name: str
    window_start: date  # already resolved later of year start / hire date
    employed_dates: frozenset[date]  # authoritative target-month service dates
    annual: BalanceSnapshot
    holiday: BalanceSnapshot
    weekly: BalanceSnapshot
    balance_usage_from: date | None = None


@dataclass(frozen=True)
class WorkbookManifest:
    department: str
    month_start: date
    employees: tuple[DepartmentWorkbookEmployee, ...]
    immutable_digest: str
    export_sha256: str
    structure_digest: str


@dataclass(frozen=True)
class ReturnedDayEvidence:
    employee_id: int
    work_date: date
    raw_value: str | None
    code: str | None
    issue: str | None
    sheet: str
    cell: str


@dataclass(frozen=True)
class WorkbookReturnPreview:
    source_sha256: str
    issues: tuple[str, ...]
    days: tuple[ReturnedDayEvidence, ...]


def _text(cell, value):
    # Spreadsheet text, never executable formula, even for =,+,-,@ name prefixes.
    cell.value = value
    cell.data_type = 's'


def _decimal(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError('Explicit finite Decimal day snapshot required.')
    if value and (value.adjusted() > 12 or value.adjusted() < -100):
        raise ValueError('Snapshot is outside supported day-display magnitude.')
    if len(value.as_tuple().digits) > 100:
        raise ValueError('Snapshot exceeds bounded decimal precision.')
    return value


def _display_decimal(value):
    # Approved planning projection only. Exact source stays in server manifest.
    _decimal(value)
    with localcontext() as context:
        context.prec = 15
        return +value


def _immutable_digest(workbook, employees, days):
    rows = []
    writable = {(FIRST_EMPLOYEE_ROW + i, d.day + 2)
                for i, employee in enumerate(employees) for d in employee.employed_dates}
    for sheet in workbook:
        for row in sheet:
            for cell in row:
                if sheet.title == ENTRY_SHEET and (cell.row, cell.column) in writable:
                    continue
                value = cell.value
                if value is not None:
                    canonical = str(Decimal(str(value)).normalize()) if cell.data_type == 'n' else str(value)
                    rows.append((sheet.title, cell.coordinate, cell.data_type, canonical))
    return sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _validate_manifest_domain(department, month_start, employees):
    if month_start.day != 1 or not department.strip() or not 1 <= len(employees) <= 300:
        raise ValueError('Explicit department, first-of-month and 1..300 employees required.')
    days = monthrange(month_start.year, month_start.month)[1]
    month_end = date(month_start.year, month_start.month, days)
    for e in employees:
        if not isinstance(e, DepartmentWorkbookEmployee):raise ValueError('Invalid manifest employee schema.')
        if not isinstance(e.code,str) or not isinstance(e.name,str):raise ValueError('Invalid employee identity text.')
        if isinstance(e.employee_id, bool) or not isinstance(e.employee_id, int) or e.employee_id < 1 or not e.code or not e.name:
            raise ValueError('Resolved employee identity required.')
        if not date(month_start.year, 1, 1) <= e.window_start <= month_end:
            raise ValueError('Explicit year-to-date/hire window required.')
        if any(not month_start <= d <= month_end or d < e.window_start for d in e.employed_dates):
            raise ValueError('Employment dates outside selected month/window.')
        if e.balance_usage_from is not None and (type(e.balance_usage_from) is not date or not month_start <= e.balance_usage_from <= month_end):
            raise ValueError('Employee balance usage basis outside target month.')
        for snapshot in (e.annual, e.holiday, e.weekly):
            for value in (snapshot.carry, snapshot.entitled_through_month_end, snapshot.used_before_month):
                _decimal(value)
            if snapshot.entitled_through_month_end < 0 or snapshot.used_before_month < 0:
                raise ValueError('Negative entitlement/usage is not a valid snapshot.')
    if len({e.employee_id for e in employees}) != len(employees) or len({e.code for e in employees}) != len(employees):
        raise ValueError('Duplicate employee identity.')


def _structure_digest(wb):
    # Semantic workflow shape, not an exact ZIP/application serialization hash.
    shape = []
    for ws in wb:
        if sum((r.max_row-r.min_row+1)*(r.max_col-r.min_col+1)
               for v in ws.data_validations.dataValidation for r in v.sqref.ranges) > 20000:
            raise ValueError('Excessive validation range.')
        validations = sorted((v.type, v.formula1, sorted(
                                 (row, col) for r in v.sqref.ranges
                                 for row in range(r.min_row, r.max_row + 1)
                                 for col in range(r.min_col, r.max_col + 1)))
                             for v in ws.data_validations.dataValidation)
        shape.append((ws.title, ws.sheet_state, bool(ws.protection.sheet),
                      sorted(str(r) for r in ws.merged_cells.ranges),
                      sorted(i for i, d in ws.row_dimensions.items() if d.hidden),
                      sorted(i for i, d in ws.column_dimensions.items() if d.hidden),
                      validations))
    return sha256(json.dumps(shape, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _inspect_container(payload):
    # Refuse ambiguous representations before openpyxl collapses them.
    from zipfile import ZipFile
    import xml.etree.ElementTree as ET
    with ZipFile(BytesIO(payload)) as archive:
        entries = archive.infolist()
        names = [i.filename for i in entries]
        if len(entries) > 2000 or len(names) != len(set(names)):
            raise ValueError('Ambiguous or excessive workbook archive entries.')
        if sum(i.file_size for i in entries) > 50_000_000:
            raise ValueError('Expanded workbook too large.')
        if any('externalLinks/' in n or n.endswith('vbaProject.bin') for n in names):
            raise ValueError('External-link or macro workbook is not a supported return.')
        for name in names:
            if not (name.endswith('.xml') or name.endswith('.rels')):
                continue
            raw = archive.read(name)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('Document declarations are not supported.')
            root = ET.fromstring(raw)
            if name.endswith('.rels') and any(r.get('TargetMode', '').lower() == 'external' for r in root):
                raise ValueError('External relationships are not supported.')
            if name == 'xl/workbook.xml':
                # Print ranges/titles are legitimate built-in names, not formulas supplied as evidence.
                for node in root.iter():
                    if node.tag.rsplit('}', 1)[-1] == 'definedName' and node.get('name') not in ('_xlnm.Print_Area', '_xlnm.Print_Titles'):
                        raise ValueError('Unexpected workbook defined name.')
            if name.startswith('xl/worksheets/') and name.endswith('.xml'):
                import re
                coordinates = set()
                for row in root.iter():
                    if row.tag.rsplit('}', 1)[-1] != 'row':
                        continue
                    row_number = row.get('r', '')
                    if not re.fullmatch(r'[1-9][0-9]{0,6}', row_number) or int(row_number) > 1048576:
                        raise ValueError('Invalid worksheet row identity.')
                    for cell in row:
                        if cell.tag.rsplit('}', 1)[-1] != 'c':
                            continue
                        coordinate = cell.get('r', '')
                        matched = re.fullmatch(r'([A-Z]{1,3})([1-9][0-9]{0,6})', coordinate)
                        if not matched or matched.group(2) != row_number:
                            raise ValueError('Invalid worksheet cell identity.')
                        from openpyxl.utils.cell import column_index_from_string
                        if column_index_from_string(matched.group(1)) > 16384:
                            raise ValueError('Invalid worksheet column identity.')
                        if coordinate in coordinates:
                            raise ValueError('Ambiguous worksheet cell coordinates.')
                        coordinates.add(coordinate)


def build_department_workbook(*, department: str, month_start: date,
                              employees: tuple[DepartmentWorkbookEmployee, ...],
                              approved_print_logo: Path | None = None,
                              balance_usage_from: date | None = None,
                              pending_manual_source: bool = False) -> tuple[bytes, WorkbookManifest]:
    _validate_manifest_domain(department, month_start, employees)
    days = monthrange(month_start.year, month_start.month)[1]
    month_end = date(month_start.year, month_start.month, days)
    if type(pending_manual_source) is not bool:
        raise ValueError('Explicit pending source marker required.')
    employee_bases = {e.balance_usage_from for e in employees if e.balance_usage_from is not None}
    if len(employee_bases) > 1:
        raise ValueError('One common usage basis required per workbook.')
    implied_basis = next(iter(employee_bases), month_start)
    if balance_usage_from is not None and employee_bases and balance_usage_from != implied_basis:
        raise ValueError('Conflicting explicit workbook usage basis.')
    usage_from = balance_usage_from or implied_basis
    if type(usage_from) is not date or not month_start <= usage_from <= month_end:
        raise ValueError('Explicit balance usage start must be inside selected month.')
    wb = Workbook();balances = wb.active;balances.title = BALANCE_SHEET
    entry = wb.create_sheet(ENTRY_SHEET)
    for ws, last_col in ((balances, 14), (entry, days + 2)):
        ws.sheet_view.rightToLeft = True
        ws.sheet_view.showGridLines = False
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        _text(ws.cell(1, 1), 'M.Y. BUSINESS HOTEL | ' + department)
        ws.cell(1, 1).font = Font(name='Arial', bold=True, size=16, color=NAVY)
        ws.row_dimensions[1].height = 58
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
        _text(ws.cell(2, 1), f'الشهر: {month_start:%Y-%m} | الرصيد المستحق حتى: {month_end:%Y-%m-%d}')
        ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=last_col)
        _text(ws.cell(3, 1), 'الأرصدة بالأيام للمتابعة والتخطيط، والعرض لخانتين تقريبي. الرصيد المعتمد بدقته الكاملة في النظام، والاستيراد لتسجيلات الأيام فقط.' + (f' استخدام الرصيد محسوب من {usage_from.isoformat()} فقط؛ تسجيل الأيام يشمل الشهر كاملًا.' if usage_from != month_start else ''))
        if pending_manual_source:
            _text(ws.cell(3, 1), 'مسودة تخطيط من أرصدة افتتاحية قيد المراجعة، ليست رصيدًا معتمدًا ولا إذنًا بالصرف. ' + (f'استخدام الرصيد من {usage_from.isoformat()} فقط، وتسجيل الأيام للشهر كاملًا.' if usage_from != month_start else 'تسجيل الأيام للشهر كاملًا.'))
        ws.row_dimensions[3].height = 48 if usage_from != month_start or pending_manual_source else 32
        ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=last_col)
        ws.row_dimensions[4].height = 30
        ws.freeze_panes = 'C7'
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = 'landscape';ws.page_setup.paperSize = ws.PAPERSIZE_A3
        ws.page_setup.fitToWidth = 1;ws.page_setup.fitToHeight = 0
        ws.print_title_rows = '1:6';ws.print_options.horizontalCentered = True
        ws.print_area = f'A1:{ws.cell(FIRST_EMPLOYEE_ROW + len(employees) + 2, last_col).column_letter}{FIRST_EMPLOYEE_ROW + len(employees) + 2}'
        ws.oddFooter.center.text = 'M.Y. BUSINESS HOTEL | &P / &N'
        ws.sheet_properties.outlinePr.summaryRight = False
        ws.protection.sheet = True;ws.protection.selectLockedCells = False;ws.protection.selectUnlockedCells = False
        if approved_print_logo is not None:
            image = Image(approved_print_logo);image.width = 54;image.height = 54
            ws.add_image(image, 'B1')
        ws.column_dimensions['A'].width = 13;ws.column_dimensions['B'].width = 28
    _text(balances.cell(4, 1), '١ الأرصدة، للقراءة فقط. المستخدم والمتاح يتحدثان من التسجيل اليومي.')
    _text(entry.cell(4, 1), '٢ التسجيل اليومي. أدخل رمزاً واحداً. الخانة الفارغة غير مسجلة، وليست حضوراً.')
    balances.merge_cells('A5:A6');balances.merge_cells('B5:B6')
    _text(balances['A5'], 'الكود');_text(balances['B5'], 'الاسم')
    for col, label in [(3, 'سنوية'), (7, 'رسمية'), (11, 'راحة أسبوعية')]:
        balances.merge_cells(start_row=5, start_column=col, end_row=5, end_column=col+3)
        _text(balances.cell(5, col), label)
        for j, field in enumerate(['مرحّل', 'مستحق حتى نهاية الشهر', 'مستخدم', 'المتاح']):
            _text(balances.cell(6, col+j), field)
            balances.column_dimensions[balances.cell(6, col+j).column_letter].width = 15 if j != 1 else 22
    _text(entry.cell(6, 1), 'الكود');_text(entry.cell(6, 2), 'الاسم')
    for day in range(1, days+1):
        entry.cell(6, day+2, day);entry.column_dimensions[entry.cell(6, day+2).column_letter].width = 4.5
    validation = DataValidation(type='list', formula1='"P,A,S,U,H,W"', allow_blank=True)
    validation.errorTitle = 'رمز غير صحيح';validation.error = 'استخدم P أو A أو S أو U أو H أو W فقط.'
    validation.showErrorMessage = True;validation.errorStyle = 'stop';entry.add_data_validation(validation)
    for i, employee in enumerate(employees):
        row = FIRST_EMPLOYEE_ROW+i
        for ws in (balances, entry):
            _text(ws.cell(row, 1), employee.code);_text(ws.cell(row, 2), employee.name)
            ws.row_dimensions[row].height = 26
        last = entry.cell(row, days+2).column_letter
        row_usage_from = employee.balance_usage_from or usage_from
        if balance_usage_from is not None and employee.balance_usage_from is not None and balance_usage_from != employee.balance_usage_from:
            raise ValueError('Conflicting workbook and employee usage basis.')
        usage_first = entry.cell(row, row_usage_from.day+2).column_letter
        for col, code, snapshot in [(3,'A',employee.annual),(7,'H',employee.holiday),(11,'W',employee.weekly)]:
            balances.cell(row,col,str(_display_decimal(snapshot.carry)));balances.cell(row,col).data_type='n'
            balances.cell(row,col+1,str(_display_decimal(snapshot.entitled_through_month_end)));balances.cell(row,col+1).data_type='n'
            balances.cell(row,col+2, f'={format(_display_decimal(snapshot.used_before_month).normalize(), "f")}+COUNTIF(\'{ENTRY_SHEET}\'!{usage_first}{row}:{last}{row},"{code}")')
            start = balances.cell(row,col).column_letter;credit=balances.cell(row,col+1).column_letter;used=balances.cell(row,col+2).column_letter
            balances.cell(row,col+3,f'={start}{row}+{credit}{row}-{used}{row}')
            for c in range(col,col+4):balances.cell(row,c).number_format = '0.00'
        for d in employee.employed_dates:
            cell=entry.cell(row,d.day+2);cell.protection=Protection(locked=False);validation.add(cell)
    legend_row = FIRST_EMPLOYEE_ROW+len(employees)+1
    for ws, last_col in ((balances,14),(entry,days+2)):
        for index, (code, label) in enumerate([('P', 'حضور'), ('A', 'سنوية'), ('S', 'مرضي'), ('U', 'غياب بدون أجر'), ('H', 'رسمية'), ('W', 'راحة أسبوعية')]):
            first = 1 + index * last_col // 6
            last = (index + 1) * last_col // 6
            for row, value in [(legend_row, label), (legend_row + 1, code)]:
                if last > first:
                    ws.merge_cells(start_row=row, start_column=first, end_row=row, end_column=last)
                _text(ws.cell(row, first), value)
                ws.row_dimensions[row].height = 23
        for row in ws:
            for cell in row:
                cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
                if cell.row not in (1,):cell.font=Font(name='Arial',size=10,color=NAVY,bold=cell.row in (5,6))
                if cell.row in (5,6):
                    cell.fill=PatternFill('solid',fgColor=NAVY);cell.font=Font(name='Arial',size=10,color='FFFFFF',bold=True)
                elif cell.row >= FIRST_EMPLOYEE_ROW and cell.row < FIRST_EMPLOYEE_ROW+len(employees):
                    cell.border=Border(left=Side(style='thin',color='D6DAE0'),right=Side(style='thin',color='D6DAE0'),top=Side(style='thin',color='D6DAE0'),bottom=Side(style='thin',color='D6DAE0'))
                    if cell.column <= 2:cell.fill=PatternFill('solid',fgColor=CREAM)
        ws.row_dimensions[6].height=34
    # No status-dependent fills. A locked empty cell means outside supplied service dates.
    wb.calculation.fullCalcOnLoad=True;wb.calculation.forceFullCalc=True
    digest = _immutable_digest(wb,employees,days)
    stream=BytesIO();wb.save(stream);payload=stream.getvalue()
    # Verify serialized display cells. Exact authoritative snapshots remain in manifest.
    check = load_workbook(BytesIO(payload), data_only=False)
    try:
        if _immutable_digest(check, employees, days) != digest:
            raise ValueError('Display projection changed during workbook serialization.')
    finally:
        check.close()
    return payload,WorkbookManifest(department,month_start,employees,digest,sha256(payload).hexdigest(),_structure_digest(wb))


def inspect_department_return(payload: bytes, manifest: WorkbookManifest) -> WorkbookReturnPreview:
    """Evidence preview only, against caller-held manifest, never workbook claims."""
    digest=sha256(payload).hexdigest()
    if len(payload)>10_000_000:raise ValueError('Workbook exceeds bounded import size.')
    _validate_manifest_domain(manifest.department, manifest.month_start, manifest.employees)
    _inspect_container(payload)
    wb=load_workbook(BytesIO(payload),data_only=False,keep_links=False)
    try:
        if wb.sheetnames != [BALANCE_SHEET,ENTRY_SHEET]:raise ValueError('Unexpected workbook sheets.')
        entry=wb[ENTRY_SHEET]
        days=monthrange(manifest.month_start.year,manifest.month_start.month)[1]
        if any(ws.max_row>FIRST_EMPLOYEE_ROW+len(manifest.employees)+10 or ws.max_column>days+15 for ws in wb):
            raise ValueError('Unexpected workbook dimensions.')
        issues=[]
        if _structure_digest(wb) != manifest.structure_digest:
            issues.append('workbook_structure_changed')
        if _immutable_digest(wb,manifest.employees,days)!=manifest.immutable_digest:
            issues.append('protected_content_changed')
        evidence=[]
        for i,employee in enumerate(manifest.employees):
            row=FIRST_EMPLOYEE_ROW+i
            for day in range(1,days+1):
                cell=entry.cell(row,day+2);raw=cell.value;work_date=date(manifest.month_start.year,manifest.month_start.month,day)
                if work_date not in employee.employed_dates:continue
                value=raw.strip().upper() if isinstance(raw,str) else raw
                code=value if value in CODES else None
                issue=None if raw is None or (isinstance(raw,str) and not raw.strip()) or code else 'invalid_day_code'
                if cell.data_type=='f':code=None;issue='formula_is_not_day_evidence'
                evidence.append(ReturnedDayEvidence(employee.employee_id,work_date,None if raw is None else str(raw),code,issue,ENTRY_SHEET,cell.coordinate))
        return WorkbookReturnPreview(digest,tuple(issues),tuple(evidence))
    finally:wb.close()
