"""Pack A / 3b: characterization tests for attendance code mapping (source 778196c).

NEW tests only. They pin CURRENT behavior of app.attendance_import,
app.fingerprint_evidence and app.department_fingerprint_review. A passing test
here means "the code does this today", NOT "this is the approved HR rule".
Each owner-decision gap is named OWNER_QUESTION_* in the test docstring.
Pure Python + openpyxl, no database, synthetic data only. The DB-bound
department_fingerprint_workflow identity-collision path is not covered here
(needs PostgreSQL at alembic head); see report.
"""
from datetime import date, datetime, time
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.attendance_import import parse_attendance_workbook
from app.department_fingerprint_review import preview_department_fingerprint_review
from app.department_workbook import ReturnedDayEvidence, WorkbookReturnPreview
from app.fingerprint_evidence import parse_fingerprint_evidence

D1 = date(2026, 11, 1)
D2 = date(2026, 11, 2)
FP_HEAD = ['AC-No.', 'Name', 'Date', 'Check Time']
PR_HEAD = ['AC-No.', 'Name', 'Date', 'Clock In', 'Clock Out']
SHA_A = 'a' * 64


def wb(rows, title='Att.log report'):
    book = Workbook()
    sheet = book.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def legacy(rows, aliases, source='fingerprint', name='x.xlsx'):
    return parse_attendance_workbook(wb(rows), name, source, aliases)


def evidence(rows, aliases):
    return parse_fingerprint_evidence(wb(rows), filename='x.xlsx', verified_aliases=aliases)


def reasons(issues):
    return [i['reason'] for i in issues]


# ---------------------------------------------------------------- two codes, one employee
def test_employee_with_two_codes_both_resolve_to_same_employee_and_keep_raw_code():
    """CURRENT: two aliases bound to employee 1 both resolve. Raw code kept per event.
    OWNER_QUESTION_1: is an employee allowed to hold two codes at once, and may
    both appear in the same month's file?"""
    rows = [FP_HEAD, ['E-01', 'x', D1, '08:00'], ['FP-9', 'x', D1, '16:00']]
    for parsed in (legacy(rows, {'E-01': 1, 'FP-9': 1}),
                   evidence(rows, {'E-01': 1, 'FP-9': 1}).occurrences):
        events = parsed['events'] if isinstance(parsed, dict) else parsed
        assert [e['employee_id'] for e in events] == [1, 1]
        assert [e['raw_employee_code'] for e in events] == ['E-01', 'FP-9']


def test_two_codes_same_instant_legacy_dedups_across_codes_evidence_keeps_both():
    """CURRENT: duplicate key is (employee_id, time, type), so the same punch under
    two codes of one employee collapses in legacy and stays in evidence mode.
    OWNER_QUESTION_2: is one punch seen under both codes one punch or two?"""
    rows = [FP_HEAD, ['E-01', 'x', D1, '08:00'], ['FP-9', 'x', D1, '08:00']]
    aliases = {'E-01': 1, 'FP-9': 1}
    old = legacy(rows, aliases)
    assert len(old['events']) == 1 and old['events'][0]['raw_employee_code'] == 'E-01'
    assert reasons(old['issues']) == ['duplicate_punch']
    new = evidence(rows, aliases)
    assert len(new.occurrences) == 2
    assert reasons(new.issues) == ['duplicate_punch']


# ---------------------------------------------------------------- one code, many employees
def test_alias_map_cannot_express_one_code_for_two_employees_case_variants_refused():
    """CURRENT: a dict key maps to one id; keys equal after normalize() (case/space/
    Arabic digits) with different ids raise employee_alias_collision. Same id is fine.
    OWNER_QUESTION_3: may a code ever be shared by two employees (e.g. reused after
    someone leaves, with dates of validity)? Code has no effective-dating."""
    rows = [FP_HEAD, ['fp', 'x', D1, '08:00']]
    with pytest.raises(ValueError, match='employee_alias_collision'):
        parse_attendance_workbook(wb(rows), 'x.xlsx', 'fingerprint', {'FP': 1, 'fp': 2})
    with pytest.raises(ValueError, match='employee_alias_collision'):
        parse_attendance_workbook(wb(rows), 'x.xlsx', 'fingerprint', {'FP 1': 1, ' fp  1': 2})
    with pytest.raises(ValueError, match='employee_alias_collision'):
        parse_attendance_workbook(wb(rows), 'x.xlsx', 'fingerprint', {'٥': 1, '5': 2})
    parsed = parse_attendance_workbook(wb(rows), 'x.xlsx', 'fingerprint', {'FP': 1, 'fp': 1})
    assert parsed['events'][0]['employee_id'] == 1


def test_evidence_adapter_refuses_case_variant_collision_before_parsing():
    """CURRENT: parse_fingerprint_evidence raises ValueError for the same collision."""
    with pytest.raises(ValueError):
        evidence([FP_HEAD, ['fp', 'x', D1, '08:00']], {'FP': 1, 'fp': 2})


def test_code_collision_is_decided_by_aliases_only_not_by_sheet_or_department_in_file():
    """CURRENT: the same code on two sheets (stand-in for two departments) resolves to
    the one employee in the alias map; no department column is read at all.
    OWNER_QUESTION_4: same code in two departments: refuse, or resolve by department?
    Today the importer cannot even see a department."""
    book = Workbook()
    first = book.active
    first.title = 'Housekeeping'
    second = book.create_sheet('Kitchen')
    for sheet, hour in ((first, '08:00'), (second, '09:00')):
        for row in (FP_HEAD, ['A-7', 'x', D1, hour]):
            sheet.append(row)
    out = BytesIO()
    book.save(out)
    parsed = parse_attendance_workbook(out.getvalue(), 'x.xlsx', 'fingerprint', {'A-7': 1})
    assert [(e['source_sheet'], e['employee_id']) for e in parsed['events']] == [
        ('Housekeeping', 1), ('Kitchen', 1)]
    assert parsed['issues'] == []


def test_same_code_same_instant_on_two_sheets_is_a_duplicate_punch_in_legacy_only():
    """CURRENT: identical code+time on two sheets becomes duplicate_punch (legacy drops
    the second, evidence keeps it)."""
    book = Workbook()
    a = book.active
    a.title = 'S1'
    b = book.create_sheet('S2')
    for sheet in (a, b):
        sheet.append(FP_HEAD)
        sheet.append(['A', 'x', D1, '08:00'])
    out = BytesIO()
    book.save(out)
    raw = out.getvalue()
    old = parse_attendance_workbook(raw, 'x.xlsx', 'fingerprint', {'A': 1})
    assert len(old['events']) == 1 and old['issues'][0]['sheet'] == 'S2'
    new = parse_fingerprint_evidence(raw, filename='x.xlsx', verified_aliases={'A': 1})
    assert len(new.occurrences) == 2


# ---------------------------------------------------------------- unknown code
def test_unknown_code_legacy_and_evidence_keep_event_unresolved_and_report():
    """CURRENT: unresolved punch kept with employee_id None + raw code; one
    unknown_employee issue per row; never fuzzy matched to a similar code/name.
    OWNER_QUESTION_5: should unknown-code punches block the month, or be parked?"""
    rows = [FP_HEAD, ['FP-1O', 'FP One', D1, '08:00 16:00']]
    for aliases in ({'FP-10': 1, 'FP One': 1},):
        old = legacy(rows, aliases)
        new = evidence(rows, aliases)
        assert [e['employee_id'] for e in old['events']] == [None, None]
        assert [e['employee_id'] for e in new.occurrences] == [None, None]
        assert old['events'][0]['raw_employee_code'] == 'FP-1O'
        assert reasons(old['issues']) == ['unknown_employee']  # one per row, not per punch
        assert reasons(new.issues) == ['unknown_employee']


def test_blank_code_name_fallback_differs_between_legacy_and_evidence_mode():
    """CURRENT: with a blank code cell legacy falls back to the NAME alias; evidence
    mode never uses the name (unknown_employee with empty value).
    OWNER_QUESTION_6: is name-only identification ever acceptable?"""
    rows = [FP_HEAD, [None, 'Yasser', D1, '08:00']]
    old = legacy(rows, {'Yasser': 2})
    assert old['events'][0]['employee_id'] == 2
    new = evidence(rows, {'Yasser': 2})
    assert new.occurrences[0]['employee_id'] is None
    assert new.issues[0]['reason'] == 'unknown_employee' and new.issues[0]['value'] == ''


def test_code_matching_is_exact_after_normalize_leading_zero_is_a_different_code():
    """CURRENT: numeric cell 5 and 5.0 equal alias '5'; text '05' does NOT; case and
    Arabic-Indic digits normalize. OWNER_QUESTION_7: are leading zeros significant?"""
    rows = [FP_HEAD, [5, 'x', D1, '08:00'], ['05', 'x', D1, '09:00'],
            [5.0, 'x', D1, '10:00'], ['٥', 'x', D1, '11:00']]
    parsed = legacy(rows, {'5': 1})
    assert [e['employee_id'] for e in parsed['events']] == [1, None, 1, 1]
    assert [i['value'] for i in parsed['issues']] == ['05']


def test_all_unknown_file_still_yields_period_for_review():
    """CURRENT: valid dates are kept even if no employee resolves."""
    parsed = legacy([FP_HEAD, ['ZZ', 'x', D2, '08:00']], {'A': 1})
    assert parsed['period_start'] == parsed['period_end'] == D2


# ---------------------------------------------------------------- direction / sequence
def test_program_report_direction_comes_from_column_not_time_order():
    """CURRENT: IN/OUT labels follow the Clock In / Clock Out columns. OUT earlier
    than IN, OUT-only, IN-only, equal times and overnight (out < in) are all kept as
    labelled, with no issue and no pairing or date roll-over.
    OWNER_QUESTION_8: what is the rule for overnight shifts and OUT-before-IN?"""
    rows = [PR_HEAD,
            ['A', 'x', D1, time(16), time(8)],
            ['A', 'x', D2, None, time(7)],
            ['A', 'x', date(2026, 11, 3), time(22), None],
            ['A', 'x', date(2026, 11, 4), time(22), time(6)],
            ['A', 'x', date(2026, 11, 5), time(8), time(8)]]
    parsed = legacy(rows, {'A': 1}, source='program_report')
    got = [(e['punch_at'].day, e['punch_at'].hour, e['event_type']) for e in parsed['events']]
    assert got == [(1, 16, 'IN'), (1, 8, 'OUT'), (2, 7, 'OUT'), (3, 22, 'IN'),
                   (4, 22, 'IN'), (4, 6, 'OUT'), (5, 8, 'IN'), (5, 8, 'OUT')]
    assert parsed['issues'] == []
    overnight = [e for e in parsed['events'] if e['punch_at'].day == 4]
    assert overnight[1]['punch_at'] < overnight[0]['punch_at']  # OUT stays on same date


def test_fingerprint_single_cell_punches_keep_file_order_and_get_no_direction():
    """CURRENT: terminal times stay in cell order (not sorted), all typed PUNCH;
    odd counts are accepted silently. OWNER_QUESTION_9: first=IN/last=OUT, or
    alternate? The code explicitly makes no direction claim."""
    rows = [FP_HEAD, ['A', 'x', D1, '16:00 08:00 12:00']]
    for events in (legacy(rows, {'A': 1})['events'], evidence(rows, {'A': 1}).occurrences):
        assert [e['punch_at'].hour for e in events] == [16, 8, 12]
        assert {e['event_type'] for e in events} == {'PUNCH'}
        assert [e['event_index'] for e in events] == [0, 1, 2]


def test_fingerprint_state_column_text_is_copied_verbatim_as_event_type():
    """CURRENT: a State/Status column is passed through unvalidated, blank -> PUNCH.
    OWNER_QUESTION_10: which state labels are valid and what do they mean?"""
    rows = [['AC-No.', 'Name', 'Date', 'Check Time', 'State'],
            ['A', 'x', D1, '16:00', 'Check Out'], ['A', 'x', D1, '08:00', 'Check In'],
            ['A', 'x', D1, '12:00', None], ['A', 'x', D1, '13:00', 'banana']]
    events = legacy(rows, {'A': 1})['events']
    assert [e['event_type'] for e in events] == ['Check Out', 'Check In', 'PUNCH', 'banana']


def test_matrix_overnight_punches_stay_on_their_calendar_day():
    """CURRENT: evidence keeps 22:00 on day 1 and 06:00 on day 2 as two separate
    calendar-date occurrences; nothing attaches 06:00 to the previous shift."""
    result = evidence([FP_HEAD, ['A', 'x', D1, '22:00'], ['A', 'x', D2, '06:00']], {'A': 1})
    assert [o['punch_at'] for o in result.occurrences] == [
        datetime(2026, 11, 1, 22), datetime(2026, 11, 2, 6)]


# ---------------------------------------------------------------- review join
def _department(days, sha=SHA_A):
    return WorkbookReturnPreview(sha, (), tuple(
        ReturnedDayEvidence(emp, day, code, code, None, 'Entry', 'C%d' % n)
        for n, (emp, day, code) in enumerate(days)))


def test_review_attaches_punches_by_resolved_employee_and_calendar_date_only():
    """CURRENT: two codes of one employee merge into that employee's day; another
    employee's punches stay separate; unknown-code punches and punches on a date the
    department sheet does not list land in unmatched_occurrences; nothing is
    eligible_for_registration and direction/shift stay unresolved."""
    ev = evidence([FP_HEAD,
                   ['E-01', 'x', D1, '08:00'], ['FP-9', 'x', D1, '16:00'],
                   ['B-2', 'y', D1, '09:00'],
                   ['ZZ', 'z', D1, '10:00'],
                   ['E-01', 'x', date(2026, 11, 9), '08:00']],
                  {'E-01': 1, 'FP-9': 1, 'B-2': 2})
    review = preview_department_fingerprint_review(
        department=_department([(1, D1, 'P'), (2, D1, 'P')]), fingerprint=ev)
    one, two = review.days
    assert [p['raw_employee_code'] for p in one.calendar_date_occurrences] == ['E-01', 'FP-9']
    assert [p['raw_employee_code'] for p in two.calendar_date_occurrences] == ['B-2']
    assert one.review_state == 'presence_needs_shift_direction_review'
    assert 'calendar_date_only_shift_and_direction_unresolved' in one.issues
    assert {p['raw_employee_code'] for p in review.unmatched_occurrences} == {'ZZ', 'E-01'}
    assert review.issues == ('unknown_employee',)
    assert not any(d.eligible_for_registration for d in review.days)
    assert not review.establishes_attendance_or_balance


def test_review_unknown_code_is_never_treated_as_absence_of_a_mapped_employee():
    """CURRENT: employee 1 coded 'U' with only an unknown-code punch is
    explicit_absence_needs_source_review; the unknown punch is unmatched.
    OWNER_QUESTION_11: who resolves an unknown code that may belong to employee 1?"""
    ev = evidence([FP_HEAD, ['ZZ', 'x', D1, '08:00']], {'A': 1})
    review = preview_department_fingerprint_review(
        department=_department([(1, D1, 'U')]), fingerprint=ev)
    assert review.days[0].review_state == 'explicit_absence_needs_source_review'
    assert len(review.unmatched_occurrences) == 1
