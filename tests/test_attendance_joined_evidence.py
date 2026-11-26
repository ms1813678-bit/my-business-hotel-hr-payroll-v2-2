"""Exact fixed-width joined HH:MM cells are readable evidence; ambiguous cells stay unreadable; a missing data row
affects only its own employee. Synthetic fixtures only. No attendance, shift or registration is decided here."""
import io
from collections import Counter
from datetime import date
from openpyxl import Workbook, load_workbook
from app.attendance_import import _evidence_time_tokens, parse_attendance_workbook
from app.department_fingerprint_review import preview_department_fingerprint_review
from app.department_workbook import ENTRY_SHEET, inspect_department_return
from app.fingerprint_evidence import parse_fingerprint_evidence
from tests.test_department_workbook import make_sample

ALIASES = {'7': 1, '8': 2, '9': 3}


def fixture():
    wb = Workbook(); ws = wb.active; ws.title = 'Att.log report'
    ws.append(['Attendance Record']); ws.append([])
    ws.append(['Att. Time', '', '2026-07-01 ~ 2026-07-31'] + [''] * 6 + ['Tabulation', '', '2026-08-02'])
    ws.append([float(d) for d in range(1, 32)])
    ws.append(['ID:', '', '7', '', '', '', '', '', 'Name:', '', 'SynOne'])
    row = [''] * 31
    row[0] = '10:2816:31'                    # exact join
    row[1] = '10:1715:2915:3015:30'          # exact join with an identical repeat
    row[2] = '10:28:3016:31'                 # seconds then a joined token: ambiguous
    row[3] = '09:3010:00:15'                 # joined HH:MM then a token with seconds: ambiguous
    row[4] = '08:0212:1'                     # fragment
    row[5] = '09:00 17:00'                   # separated, already fine
    ws.append(row)
    ws.append(['ID:', '', '8', '', '', '', '', '', 'Name:', '', 'SynTwo'])
    row = [''] * 31; row[0] = '07:5716:03'; ws.append(row)
    ws.append(['ID:', '', '9', '', '', '', '', '', 'Name:', '', 'SynThree'])      # last block: header with no data row
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


def test_exact_joins_are_readable_and_ambiguous_cells_stay_unreadable():
    assert _evidence_time_tokens('10:2816:31') == ([__import__('datetime').time(10, 28), __import__('datetime').time(16, 31)], False)
    assert _evidence_time_tokens('09:00 17:00')[1] is False
    for bad in ('10:28:3016:31', '09:3010:00:15', '08:0212:1', '25:00', '10:2816:3'):
        assert _evidence_time_tokens(bad)[1] is True, bad
    parsed = parse_attendance_workbook(fixture(), 'f.xlsx', 'fingerprint', ALIASES, preserve_duplicate_punches=True)
    invalid = {(i['employee_id'], i['work_date'].day, i['value']) for i in parsed['issues'] if i['reason'] == 'invalid_time_value'}
    assert invalid == {(1, 3, '10:28:3016:31'), (1, 4, '09:3010:00:15'), (1, 5, '08:0212:1')}
    kept = [e for e in parsed['events'] if e['employee_id'] == 1 and e['punch_at'].day in (1, 2)]
    assert [e['raw_timestamp'] for e in kept] == ['10:2816:31'] * 2 + ['10:1715:2915:3015:30'] * 4     # raw text and all 6 occurrences kept
    assert sum(1 for i in parsed['issues'] if i['reason'] == 'duplicate_punch') == 1                      # the repeat is still shown


def review():
    payload, manifest = make_sample(date(2026, 7, 1), count=3)
    wb = load_workbook(io.BytesIO(payload)); ws = wb[ENTRY_SHEET]
    for employee in range(3):
        for day in range(1, 7):
            ws.cell(7 + employee, day + 2).value = 'P'
    out = io.BytesIO(); wb.save(out)
    department = inspect_department_return(out.getvalue(), manifest)
    fingerprint = parse_fingerprint_evidence(fixture(), filename='f.xlsx', verified_aliases=ALIASES)
    return preview_department_fingerprint_review(department=department, fingerprint=fingerprint)


def test_review_states_follow_readability_and_a_missing_block_stays_with_its_employee():
    days = {(d.employee_id, d.department_work_date.day): d for d in review().days}
    unreadable = 'unreadable_fingerprint_evidence_needs_review'
    assert days[(1, 1)].review_state == days[(1, 2)].review_state == 'presence_needs_shift_direction_review'
    assert 'duplicate_occurrences_retained' in days[(1, 2)].issues
    assert days[(1, 3)].review_state == days[(1, 4)].review_state == days[(1, 5)].review_state == unreadable
    assert days[(1, 6)].review_state == 'presence_needs_shift_direction_review'
    assert days[(2, 1)].review_state == 'presence_needs_shift_direction_review'          # other employee untouched
    assert all(days[(3, n)].review_state == unreadable for n in range(1, 7))               # only the empty block
    assert all(not d.eligible_for_registration for d in days.values())
    states = Counter(d.review_state for d in review().days)
    assert states[unreadable] == 3 + 31 and review().establishes_attendance_or_balance is False
