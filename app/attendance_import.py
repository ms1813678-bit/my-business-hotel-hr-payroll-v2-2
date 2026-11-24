"""Read attendance exports from the hotel attendance program and fingerprint terminal.

This module parses evidence only. It never writes to the database, changes source cells,
or infers employee identities by fuzzy matching.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import csv
import math
from io import BytesIO
from pathlib import Path
import re
import unicodedata


DATE_HEADERS = {"date", "work date", "日期", "التاريخ", "اليوم", "تاريخ"}
CODE_HEADERS = {"ac-no", "ac-no.", "ac no", "ac no.", "employee id", "user id", "pin", "employee code", "id", "رقم الموظف", "كود الموظف", "رقم", "كود"}
NAME_HEADERS = {"name", "employee name", "الاسم", "اسم الموظف", "اسم"}
IN_HEADERS = {"clock in", "check in", "in time", "وقت الحضور", "دخول", "وقت الدخول"}
OUT_HEADERS = {"clock out", "check out", "out time", "وقت الانصراف", "خروج", "وقت الخروج"}
EVENT_HEADERS = {"check time", "punch time", "attendance time", "att. time", "time", "وقت البصمة", "وقت", "توقيت البصمة"}
EVENT_TYPE_HEADERS = {"state", "status", "event type", "type", "الحالة", "نوع الحركة"}
TIME_TOKEN = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)")


def normalize(value) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    return re.sub(r"\s+", " ", text)


def _date_value(value, datemode=0):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and value > 0:
        try:
            import xlrd
            return xlrd.xldate_as_datetime(value, datemode).date()
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None



def _evidence_date_value(value, datemode=0):
    # No locale/date-order policy is inferred in the review adapter.
    if isinstance(value,str):
        raw=value.strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw) is None:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return _date_value(value,datemode)


def _time_tokens(value):
    if isinstance(value, datetime):
        return [value.time().replace(microsecond=0)]
    if isinstance(value, time):
        return [value.replace(microsecond=0)]
    if isinstance(value, (int, float)) and 0 <= value < 1:
        minutes = round(value * 1440) % 1440
        return [time(minutes // 60, minutes % 60)]
    if value is None or str(value).strip() == "":
        return []
    raw = str(value).strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    matches = list(TIME_TOKEN.finditer(raw))
    parsed = [
        time(int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
        for match in matches
    ]
    if parsed:
        return parsed
    try:
        return [time.fromisoformat(raw)]
    except ValueError:
        return []


RAW_MAX_ROWS = 20000
RAW_MAX_COLUMNS = 128
RAW_MAX_CELLS = 300000
RAW_MAX_EVENTS = 200000
RAW_MAX_ISSUES = 20000


class _BoundedEvidenceList(list):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def append(self, value):
        if len(self) >= self.limit:
            raise ValueError('fingerprint_evidence_scan_limit')
        super().append(value)

    def extend(self, values):
        for value in values:
            self.append(value)


def _load_rows(file_bytes: bytes, filename: str, *, bounded=False):
    suffix = Path(filename or "").suffix.casefold()
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        raw_max_column = 1
        if bounded:
            # Worksheet dimensions are untrusted hints. Inspect actual cells
            # before openpyxl can omit rows or expand sparse padded ranges.
            from zipfile import ZipFile
            from xml.etree.ElementTree import iterparse
            from openpyxl.utils.cell import coordinate_to_tuple
            actual_cells = 0
            with ZipFile(BytesIO(file_bytes)) as archive:
                members = archive.infolist()
                if len(members)>3000 or sum(m.file_size for m in members)>60000000:
                    raise ValueError('fingerprint_evidence_scan_limit')
                for member in members:
                    if not member.filename.startswith('xl/worksheets/') or not member.filename.endswith('.xml'):
                        continue
                    with archive.open(member) as source:
                        for _event, element in iterparse(source, events=('end',)):
                            if element.tag.rsplit('}',1)[-1]=='c':
                                actual_cells += 1
                                try:
                                    row_number,column_number=coordinate_to_tuple(element.attrib['r'])
                                except (KeyError,ValueError,TypeError) as exc:
                                    raise ValueError('invalid_fingerprint_cell_coordinate') from exc
                                if (row_number<1 or column_number<1 or row_number>RAW_MAX_ROWS or column_number>RAW_MAX_COLUMNS
                                        or actual_cells>RAW_MAX_CELLS):
                                    raise ValueError('fingerprint_evidence_scan_limit')
                                raw_max_column=max(raw_max_column,column_number)
                            element.clear()
        workbook = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
        try:
            result = []
            cells = 0
            if bounded and len(workbook.worksheets) > 32:
                raise ValueError('fingerprint_evidence_scan_limit')
            for sheet in workbook.worksheets:
                if bounded:
                    sheet.reset_dimensions()
                rows = []
                for row in sheet.iter_rows(values_only=True, **({'max_col':raw_max_column} if bounded else {})):
                    cells += len(row)
                    if bounded and (len(rows) >= RAW_MAX_ROWS or len(row) > RAW_MAX_COLUMNS or cells > RAW_MAX_CELLS):
                        raise ValueError('fingerprint_evidence_scan_limit')
                    rows.append(row)
                result.append((sheet.title, rows, 0))
            return result
        finally:
            workbook.close()
    if suffix == ".csv":
        decoded = None
        for encoding in ("utf-8-sig", "utf-8", "cp1256"):
            try:
                decoded = file_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError("csv_encoding_not_supported")
        try:
            sample = decoded[:8192]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                counts = {delimiter: sample.count(delimiter) for delimiter in (",", ";", "\t", "|")}
                delimiter = max(counts, key=counts.get)
                if counts[delimiter] == 0:
                    raise ValueError("csv_delimiter_not_detected")
                dialect = csv.excel()
                dialect.delimiter = delimiter
            rows = []
            cells = 0
            for row in csv.reader(decoded.splitlines(), dialect, strict=True):
                cells += len(row)
                if bounded and (len(rows) >= RAW_MAX_ROWS or len(row) > RAW_MAX_COLUMNS or cells > RAW_MAX_CELLS):
                    raise ValueError('fingerprint_evidence_scan_limit')
                rows.append(tuple(row))
        except (csv.Error, UnicodeError) as exc:
            raise ValueError("invalid_csv_file") from exc
        if not rows or not any(any(str(cell).strip() for cell in row) for row in rows):
            raise ValueError("empty_attendance_file")
        return [("CSV", rows, 0)]
    if suffix == ".xls":
        try:
            import xlrd
        except ImportError as exc:
            raise ValueError("legacy_xls_support_not_installed") from exc
        workbook = xlrd.open_workbook(file_contents=file_bytes, on_demand=True)
        try:
            if bounded and (workbook.nsheets > 32 or any(
                    sheet.nrows > RAW_MAX_ROWS or sheet.ncols > RAW_MAX_COLUMNS
                    for sheet in workbook.sheets()) or sum(
                    sheet.nrows * sheet.ncols for sheet in workbook.sheets()) > RAW_MAX_CELLS):
                raise ValueError('fingerprint_evidence_scan_limit')
            return [
                (sheet.name, [sheet.row_values(i) for i in range(sheet.nrows)], workbook.datemode)
                for sheet in workbook.sheets()
            ]
        finally:
            workbook.release_resources()
    raise ValueError("unsupported_attendance_file_extension")


def _evidence_time_tokens(value):
    """Retain valid tokens and explicitly flag any unreadable cell remainder."""
    if isinstance(value, (datetime, time)):
        return _time_tokens(value), bool(value.microsecond)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 0 <= value < 1:
            seconds=min(86399, round(value * 86400))
            return [time(seconds//3600,(seconds%3600)//60,seconds%60)], abs(value * 86400 - seconds) > 0.000001
        return [], True
    raw=str(value or '').strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    if len(raw) > 16384:
        raise ValueError('fingerprint_evidence_scan_limit')
    if not raw: return [], False
    pattern=re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?')
    tokens=[]; invalid=False; position=0
    while position<len(raw):
        if raw[position].isspace() or raw[position] in ',;|':
            position+=1; continue
        match=pattern.match(raw,position)
        if match is not None:
            tokens.append(time.fromisoformat(match.group()))
            position=match.end()
            if position<len(raw) and raw[position].isdigit():
                invalid=True  # Adjacent tokens retained, but ambiguous source remains visible.
        else:
            invalid=True
            # Preserve valid separated tokens without reinterpreting a suffix
            # of an invalid token such as25:00 as a different valid time.
            while position<len(raw) and not raw[position].isspace() and raw[position] not in ',;|':
                position+=1
    return tokens, invalid


def _matrix_time_tokens(value):
    """Parse separated or directly concatenated HH:MM tokens without guessing."""
    if isinstance(value, (datetime, time)):
        return _time_tokens(value)
    raw = str(value or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    if not raw:
        return []
    token_pattern = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?")
    tokens = []
    position = 0
    while position < len(raw):
        if raw[position].isspace() or raw[position] in {",", ";", "|"}:
            position += 1
            continue
        match = token_pattern.match(raw, position)
        if match is None:
            return []
        try:
            tokens.append(time.fromisoformat(match.group(0)))
        except ValueError:
            return []
        position = match.end()
    return tokens


def _parse_att_log_matrix(sheet_name: str, rows: list, aliases: dict, *, raw_evidence=False):
    """Parse the fingerprint terminal's day-columns / employee-block report layout."""
    # The same workbook contains summary and schedule sheets with date ranges;
    # only the raw Att. Time matrix is an event source.
    has_att_time_marker = any(
        normalize(value) in {"att. time", "att time"}
        for row in rows[:8] for value in row
    )
    if not has_att_time_marker:
        return None
    period_match = None
    for row in rows[:8]:
        for value in row:
            if value is None:
                continue
            match = re.search(
                r"(\d{4}-\d{2}-\d{2})\s*(?:~|–|\bto\b)\s*(\d{4}-\d{2}-\d{2})",
                str(value), re.IGNORECASE,
            )
            if match:
                try:
                    period_match = (
                        datetime.strptime(match.group(1), "%Y-%m-%d").date(),
                        datetime.strptime(match.group(2), "%Y-%m-%d").date(),
                    )
                except ValueError:
                    period_match = None
                break
        if period_match:
            break
    if period_match is None:
        return None
    period_start, period_end = period_match
    if period_end < period_start or period_start.day != 1 or (
        period_start.year, period_start.month
    ) != (period_end.year, period_end.month):
        return {
            "events": [], "issues": [{
                "sheet": sheet_name, "row": 1,
                "reason": "invalid_or_missing_date", "value": str(period_match),
            }], "dates": [period_start, period_end],
        }

    day_header_index = None
    day_columns = {}
    for row_index, row in enumerate(rows[:10]):
        candidate = {}
        for column_index, value in enumerate(row):
            if raw_evidence and isinstance(value,(int,float)) and (isinstance(value,bool) or not math.isfinite(value)):
                continue
            if isinstance(value, (int, float)) and int(value) == value:
                day = int(value)
            elif isinstance(value, str) and value.strip().isdigit():
                day = int(value.strip())
            else:
                continue
            if 1 <= day <= 31 and day not in candidate.values():
                candidate[column_index] = day
        expected_days = (period_end - period_start).days + 1
        if set(candidate.values()) == set(range(1, expected_days + 1)):
            day_header_index, day_columns = row_index, candidate
            break
    if day_header_index is None:
        return {
            "events": [], "issues": [{
                "sheet": sheet_name, "row": 1,
                "reason": "missing_day_columns", "value": "day columns do not cover report period",
            }], "dates": [period_start, period_end],
        }

    events = _BoundedEvidenceList(RAW_MAX_EVENTS) if raw_evidence else []
    issues = _BoundedEvidenceList(RAW_MAX_ISSUES) if raw_evidence else []
    for header_index, row in enumerate(rows):
        id_marker = next(
            (index for index, value in enumerate(row) if normalize(value) in {"id:", "id"}),
            None,
        )
        if id_marker is None:
            continue
        name_marker = next(
            (index for index, value in enumerate(row) if normalize(value) in {"name:", "name"}),
            None,
        )

        def first_after(index):
            if index is None:
                return None
            for value in row[index+1:]:
                if raw_evidence and normalize(value) in {'id:', 'id', 'name:', 'name'}:
                    return None
                if value is not None and str(value).strip():
                    return value
            return None

        raw_code = first_after(id_marker)
        raw_name = first_after(name_marker)
        identity_name = None if raw_evidence else raw_name
        if raw_evidence and isinstance(raw_code,(int,float)) and (isinstance(raw_code,bool) or not math.isfinite(raw_code)):
            issues.append({'reason':'invalid_employee_identity','sheet':sheet_name,'row':header_index+1,
                'employee_id':None,'work_date':None,'raw_employee_code':str(raw_code),'raw_employee_name':str(raw_name)})
            raw_code = None
            raw_name = None  # Invalid supplied identity must not silently fall back to name.
        if header_index + 1 >= len(rows):
            if raw_evidence:
                issues.append({'reason':'missing_employee_data_row', 'sheet':sheet_name,
                    'row':header_index+1, 'employee_id':aliases.get(str(int(raw_code)) if isinstance(raw_code, (int,float)) and not isinstance(raw_code,bool) and raw_code == int(raw_code) else normalize(raw_code if raw_code not in (None, '') else identity_name)), 'work_date':None,
                    'raw_employee_code':str(raw_code), 'raw_employee_name':str(raw_name)})
            continue
        data_row_index = header_index + 1
        data_row = rows[data_row_index]
        # Do not consume another employee's metadata row as punch data.
        if any(normalize(value) in {"id:", "id"} for value in data_row):
            if raw_evidence:
                issues.append({'reason':'missing_employee_data_row', 'sheet':sheet_name,
                    'row':header_index+1, 'employee_id':aliases.get(str(int(raw_code)) if isinstance(raw_code, (int,float)) and not isinstance(raw_code,bool) and raw_code == int(raw_code) else normalize(raw_code if raw_code not in (None, '') else identity_name)), 'work_date':None,
                    'raw_employee_code':str(raw_code), 'raw_employee_name':str(raw_name)})
            continue

        identity = raw_code if raw_code not in (None, "") else identity_name
        # xlrd exposes numeric .xls identifiers as floats (for example 1.0),
        # while employee aliases are stored as their canonical integer text.
        identity_key = (
            str(int(identity))
            if isinstance(identity, (int, float)) and int(identity) == identity
            else normalize(identity)
        )
        employee_id = aliases.get(identity_key)
        row_events = _BoundedEvidenceList(RAW_MAX_EVENTS) if raw_evidence else []
        event_index = 0
        for column_index, day in day_columns.items():
            raw_value = data_row[column_index] if column_index < len(data_row) else None
            if raw_value in (None, ""):
                continue
            work_date = period_start + timedelta(days=day - 1)
            tokens, unreadable = _evidence_time_tokens(raw_value) if raw_evidence else (_matrix_time_tokens(raw_value), False)
            if raw_evidence and isinstance(raw_value, datetime) and raw_value.date() != work_date:
                tokens, unreadable = [], True
            if not tokens or unreadable:
                issues.append({
                    "sheet": sheet_name, "row": data_row_index + 1,
                    "reason": "invalid_time_value", "value": str(raw_value),
                    "employee_id": employee_id, "work_date": work_date,
                    "raw_employee_code": str(raw_code), "raw_employee_name": str(raw_name),
                })
                if not tokens: continue
            for punch_time in tokens:
                row_events.append({
                    "employee_id": employee_id,
                    "raw_employee_code": str(raw_code) if raw_code not in (None, "") else None,
                    "raw_employee_name": str(raw_name) if raw_name not in (None, "") else None,
                    "punch_at": datetime.combine(work_date, punch_time),
                    "raw_timestamp": str(raw_value),
                    "event_type": "PUNCH",
                    "source_sheet": sheet_name,
                    "source_row": data_row_index + 1,
                    "event_index": event_index,
                })
                event_index += 1
        if employee_id is None and row_events:
            issues.append({
                "sheet": sheet_name, "row": data_row_index + 1,
                "reason": "unknown_employee", "value": str(identity or ""),
            })
        events.extend(row_events)

    return {
        "events": events,
        "issues": issues,
        "dates": [period_start, period_end],
    }


def parse_attendance_workbook(file_bytes: bytes, filename: str, source_type: str, employee_aliases: dict, *,
                              preserve_duplicate_punches: bool = False):
    """Return raw-positioned punch events and issues for HR review.

    employee_aliases maps exact employee codes, fingerprint IDs, and names to employee IDs.
    Program reports contribute separate in/out events; fingerprint exports preserve every
    time token in event cells (including multiple events in a single cell).
    """
    if type(preserve_duplicate_punches) is not bool:
        raise ValueError('Explicit duplicate preservation flag required.')
    if preserve_duplicate_punches and source_type != 'fingerprint':
        raise ValueError('Raw occurrence preservation is fingerprint-only.')
    if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
        raise ValueError("empty_attendance_file")
    if source_type not in {"program_report", "fingerprint"}:
        raise ValueError("unsupported_attendance_source_type")
    aliases = {}
    for alias, employee_id in employee_aliases.items():
        key = normalize(alias)
        if not key or key in aliases and aliases[key] != employee_id:
            raise ValueError("employee_alias_collision")
        aliases[key] = employee_id
    if not aliases:
        raise ValueError("employee_aliases_required")

    if preserve_duplicate_punches:
        try:
            from lxml.etree import XMLSyntaxError
            xml_errors=(XMLSyntaxError,)
        except ImportError:
            xml_errors=()
        try:
            sheets = _load_rows(bytes(file_bytes), filename, bounded=True)
        except (OSError, AssertionError, *xml_errors) as exc:
            # Decoder boundary only. Never hide errors in identity/join/storage.
            raise ValueError('invalid_fingerprint_workbook_decode') from exc
    else:
        sheets = _load_rows(bytes(file_bytes), filename)

    events = _BoundedEvidenceList(RAW_MAX_EVENTS) if preserve_duplicate_punches else []
    issues = _BoundedEvidenceList(RAW_MAX_ISSUES) if preserve_duplicate_punches else []
    dates = []
    for sheet_name, rows, datemode in sheets:
        header_index = None
        columns = {}
        for row_index, row in enumerate(rows):
            normalized = [normalize(cell) for cell in row]
            found = {
                "date": [i for i, cell in enumerate(normalized) if cell in DATE_HEADERS],
                "code": [i for i, cell in enumerate(normalized) if cell in CODE_HEADERS],
                "name": [i for i, cell in enumerate(normalized) if cell in NAME_HEADERS],
                "in": [i for i, cell in enumerate(normalized) if cell in IN_HEADERS],
                "out": [i for i, cell in enumerate(normalized) if cell in OUT_HEADERS],
                "event": [i for i, cell in enumerate(normalized) if cell in EVENT_HEADERS],
                "type": [i for i, cell in enumerate(normalized) if cell in EVENT_TYPE_HEADERS],
            }
            if len(found["date"]) == 1 and (found["code"] or found["name"]) and (
                source_type == "program_report" and found["in"] and found["out"]
                or source_type == "fingerprint" and (found["event"] or found["in"] or found["out"])
            ):
                header_index = row_index
                columns = {key: value[0] for key, value in found.items() if value}
                break
        if header_index is None:
            if source_type == "fingerprint":
                matrix_result = _parse_att_log_matrix(sheet_name, rows, aliases, raw_evidence=preserve_duplicate_punches)
                if matrix_result is not None:
                    events.extend(matrix_result["events"])
                    issues.extend(matrix_result["issues"])
                    dates.extend(matrix_result["dates"])
                    continue
            continue

        for row_index in range(header_index + 1, len(rows)):
            row = rows[row_index]
            if not any(value is not None and str(value).strip() for value in row):
                continue
            if Path(filename or "").suffix.casefold() == ".csv" and len(row) != len(rows[header_index]):
                issues.append({
                    "sheet": sheet_name, "row": row_index + 1,
                    "reason": "invalid_row_column_count",
                    "employee_id": aliases.get(normalize(row[columns['code']]
                        if 'code' in columns and columns['code'] < len(row) and row[columns['code']] not in (None, '')
                        else row[columns['name']] if not preserve_duplicate_punches and 'name' in columns and columns['name'] < len(row) else None)),
                    "work_date": (_evidence_date_value if preserve_duplicate_punches else _date_value)(row[columns['date']], datemode) if columns['date'] < len(row) else None,
                    "raw_row": str(row),
                    "value": f"expected {len(rows[header_index])} columns, got {len(row)}",
                })
                continue
            def cell(key):
                index = columns.get(key)
                return row[index] if index is not None and index < len(row) else None

            employee_key = normalize(cell("code")) if cell("code") not in (None, "") else ("" if preserve_duplicate_punches else normalize(cell("name")))
            employee_id = aliases.get(employee_key)
            raw_date = cell("date")
            work_date = (_evidence_date_value if preserve_duplicate_punches else _date_value)(raw_date, datemode)
            if work_date is None:
                issues.append({"sheet": sheet_name, "row": row_index + 1, "reason": "invalid_or_missing_date", "value": str(raw_date), "employee_id":employee_id, "work_date":None, "raw_employee_code":str(cell("code")), "raw_employee_name":str(cell("name")), "raw_time_value":str(cell("event") or cell("in") or cell("out") or "")})
                continue
            # Keep valid source dates even when the employee cannot be resolved.
            # Otherwise an all-unknown-employee file loses its period and cannot
            # enter the HR review workflow as an import batch.
            dates.append(work_date)
            if employee_id is None:
                issues.append({
                    "sheet": sheet_name, "row": row_index + 1,
                    "reason": "unknown_employee", "value": str(employee_key),
                    "employee_id": None, "work_date": work_date,
                })

            values = []
            if source_type == "program_report":
                values = [("IN", cell("in")), ("OUT", cell("out"))]
            else:
                event_value = cell("event")
                if event_value not in (None, ""):
                    values = [(str(cell("type") or "PUNCH"), event_value)]
                else:
                    values = [("IN", cell("in")), ("OUT", cell("out"))]

            event_index = 0
            for event_type, raw_value in values:
                if raw_value in (None, ""):
                    continue
                tokens, unreadable = _evidence_time_tokens(raw_value) if preserve_duplicate_punches else (_time_tokens(raw_value), False)
                if preserve_duplicate_punches and isinstance(raw_value, datetime) and raw_value.date() != work_date:
                    tokens, unreadable = [], True
                if not tokens or unreadable:
                    issues.append({"sheet": sheet_name, "row": row_index + 1, "reason": "invalid_time_value", "value": str(raw_value),
                        "employee_id":employee_id, "work_date":work_date, "raw_employee_code":str(cell("code")), "raw_employee_name":str(cell("name"))})
                    if not tokens: continue
                for punch_time in tokens:
                    punch_at = datetime.combine(work_date, punch_time)
                    events.append({
                        "employee_id": employee_id,
                        "raw_employee_code": str(cell("code")) if cell("code") not in (None, "") else None,
                        "raw_employee_name": str(cell("name")) if cell("name") not in (None, "") else None,
                        "punch_at": punch_at,
                        "raw_timestamp": str(raw_value),
                        "event_type": event_type,
                        "source_sheet": sheet_name,
                        "source_row": row_index + 1,
                        "event_index": event_index,
                    })
                    event_index += 1
    if not events and not issues:
        raise ValueError("attendance_headers_not_found")
    seen_events = set()
    unique_events = []
    for event in events:
        employee_identity = event.get("employee_id")
        if employee_identity is None:
            employee_identity = (
                "unresolved",
                normalize(event.get("raw_employee_code") or event.get("raw_employee_name")),
            )
        identity = (employee_identity, event["punch_at"], event.get("event_type"))
        if identity in seen_events:
            issues.append({
                "sheet": event.get("source_sheet"),
                "row": event.get("source_row"),
                "reason": "duplicate_punch",
                "employee_id": event.get('employee_id'),
                "work_date": event['punch_at'].date(),
                "value": event.get("raw_timestamp", ""),
            })
            continue
        seen_events.add(identity)
        unique_events.append(event)
    if not preserve_duplicate_punches:
        events = unique_events
    return {
        "source_type": source_type,
        "events": events,
        "issues": issues,
        "period_start": min(dates) if dates else None,
        "period_end": max(dates) if dates else None,
        "sheet_count": len(sheets),
    }
