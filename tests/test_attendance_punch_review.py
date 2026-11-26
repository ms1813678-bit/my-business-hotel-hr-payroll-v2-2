"""Joined HH:MM split and duplicate-punch review over a synthetic fixture shaped like the terminal's Att.log report.
No real hotel file is used or stored."""
import io
from openpyxl import Workbook
import pytest
from app.attendance_import import parse_attendance_workbook
from app.attendance_punch_review import build_punch_review, split_joined_hhmm

ALIASES = {'7': 1, 'SyntheticOne': 1, '8': 2, 'SyntheticTwo': 2}


def fixture_bytes():
    wb = Workbook(); ws = wb.active; ws.title = 'Att.log report'
    ws.append(['Attendance Record']); ws.append([])
    ws.append(['Att. Time', '', '2026-07-01 ~ 2026-07-31'] + [''] * 6 + ['Tabulation', '', '2026-08-02'])
    ws.append([float(d) for d in range(1, 32)])
    ws.append(['ID:', '', '7', '', '', '', '', '', 'Name:', '', 'SyntheticOne'])
    row = [''] * 31
    row[1] = '10:2816:31'                    # joined, exact
    row[2] = '09:00'                         # single punch
    row[3] = '08:0212:0612:0612:0612:1'      # triple duplicate then a truncated fragment
    row[7] = '10:1715:2915:3015:30'          # one exact duplicate (15:30)
    row[8] = '10:28:3016:31'                 # seconds: ambiguous against joined HH:MM
    ws.append(row)
    ws.append(['ID:', '', '8', '', '', '', '', '', 'Name:', '', 'SyntheticTwo'])
    row = [''] * 31; row[0] = '07:5716:03'; row[5] = '16:02'; row[9] = '99:99'
    ws.append(row)
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


def parsed():
    return parse_attendance_workbook(fixture_bytes(), 'f.xlsx', 'fingerprint', ALIASES, preserve_duplicate_punches=True)


def test_split_is_exact_for_fixed_width_and_refuses_ambiguity():
    s = split_joined_hhmm('10:2816:31')
    assert [t.strftime('%H:%M') for t in s['tokens']] == ['10:28', '16:31'] and s['exact'] and s['remainder'] == ''
    assert split_joined_hhmm('09:00 17:00,23:59')['exact']
    for bad, rem in (('10:28:3016:31', '10:28:3016:31'), ('12:1', '12:1'), ('25:00', '25:00'), ('abc', 'abc')):
        r = split_joined_hhmm(bad)
        assert not r['exact'] and r['remainder'] == rem
    assert split_joined_hhmm('08:0212:1')['remainder'] == '12:1' and not split_joined_hhmm('08:0212:1')['exact']


def test_every_occurrence_is_kept_duplicates_are_shown_and_nothing_is_decided():
    p = parsed()
    rep = build_punch_review(p)
    assert rep['dropped_occurrences'] == 0 and rep['attendance_decided'] is False and rep['registration_allowed'] is False
    assert sum(len(d['occurrences']) for d in rep['days']) + rep['summary']['unmatched_identity_occurrences'] == len(p['events'])
    by = {(d['employee_id'], d['work_date']): d for d in rep['days']}
    assert by[(1, '2026-07-02')]['review_state'] == 'joined_exact_preserved' and by[(1, '2026-07-02')]['joined_exact']
    assert by[(1, '2026-07-03')]['review_state'] == 'single_punch_preserved'
    d4 = by[(1, '2026-07-04')]
    assert d4['review_state'] == 'unreadable_needs_hr_review' and d4['unreadable_remainder'] == '12:1'
    assert d4['duplicates'] == [{'time': '12:06:00', 'count': 3}] and len(d4['occurrences']) == 4     # all four read punches kept
    d8 = by[(1, '2026-07-08')]
    assert d8['review_state'] == 'duplicate_needs_hr_review' and d8['duplicates'] == [{'time': '15:30:00', 'count': 2}]
    assert len(d8['occurrences']) == 4
    assert by[(1, '2026-07-09')]['review_state'] == 'unreadable_needs_hr_review'                       # seconds are ambiguous
    assert by[(2, '2026-07-10')]['review_state'] == 'unreadable_needs_hr_review' and by[(2, '2026-07-10')]['occurrences'] == []
    assert by[(2, '2026-07-01')]['review_state'] == 'joined_exact_preserved'
    assert rep['summary']['other_source_issues'] == {}
    extra = parsed(); extra['issues'].append({'reason': 'missing_employee_data_row', 'sheet': 'Att.log report', 'row': 9})
    assert build_punch_review(extra)['summary']['other_source_issues'] == {'missing_employee_data_row': 1}     # surfaced, never hidden
    assert rep['summary']['duplicate_extra_occurrences'] == 3          # two extra 12:06, one extra 15:30


def test_report_is_stable_and_refuses_malformed_input():
    assert build_punch_review(parsed()) == build_punch_review(parsed())
    with pytest.raises(ValueError):
        build_punch_review({'events': 1, 'issues': []})
    with pytest.raises(ValueError):
        split_joined_hhmm(None)
