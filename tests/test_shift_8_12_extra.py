"""Pack B / 3t: NEW pin tests for 8h/12h shift boundaries (source 778196c).

Pins CURRENT behavior only. Synthetic data. No core change. The first three
sections need no database. The last section needs PostgreSQL at alembic head
and is skipped (UNRAN) when no database is reachable.
"""
import ast
import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from app.attendance_reconciliation import (
    infer_non_security_shift, reconcile_attendance_day, shift_window, work_date_for_punch)
from app.fingerprint_evidence import FingerprintEvidence
from app.shift_evidence_preview import ExplicitShiftWindow, preview_shift_occurrences
from app.shift_matching import match_shift_by_clock

ROOT = Path(__file__).resolve().parents[1]
D = date(2026, 11, 1)


def pat(identifier, start, end, before=120, after=120):
    return dict(id=identifier, name=identifier, start_time=time.fromisoformat(start),
                end_time=time.fromisoformat(end), match_before_minutes=before, match_after_minutes=after)


# Seeded shapes (scripts/seed_shifts.py), margins at the seeded default 120/120.
RECEPTION = [pat('REC-0700', '07:00', '15:00'), pat('REC-1500', '15:00', '23:00'), pat('REC-2300', '23:00', '07:00')]
SECURITY = [pat('SEC-0800-2000', '08:00', '20:00'), pat('SEC-2000-0800', '20:00', '08:00'),
            pat('SEC-0800-1600', '08:00', '16:00'), pat('SEC-1600-0000', '16:00', '00:00'),
            pat('SEC-0000-0800', '00:00', '08:00')]


def at(h, m=0, day=1):
    return datetime(2026, 11, day, h, m)


def m(patterns, h, mi=0, day=1):
    return match_shift_by_clock(at(h, mi, day), patterns)


# ---------------------------------------------------------------- matcher (pure)

def test_8h_reception_inclusive_window_edges_and_dead_zones():
    # 8h gaps: 480 min, margins capped at 120, so edges are inclusive at +-120.
    assert m(RECEPTION, 5, 0)['shift']['id'] == 'REC-0700'      # exactly -120
    assert m(RECEPTION, 4, 59)['status'] == 'unmatched'          # 121 early
    assert m(RECEPTION, 9, 0)['shift']['id'] == 'REC-0700'      # exactly +120
    assert m(RECEPTION, 9, 1)['status'] == 'unmatched'           # dead zone 09:01..12:59
    assert m(RECEPTION, 12, 59)['status'] == 'unmatched'
    assert m(RECEPTION, 13, 0)['shift']['id'] == 'REC-1500'
    assert m(RECEPTION, 9, 1)['reason'] == 'outside_non_overlapping_matching_window'


def test_overnight_start_matches_by_clock_time_only_across_midnight():
    # REC-2300: 22:00..01:00 by time of day. The date of the punch plays no part.
    for h, mi in ((21, 0), (23, 0), (0, 59), (1, 0)):
        assert m(RECEPTION, h, mi)['shift']['id'] == 'REC-2300', (h, mi)
    assert m(RECEPTION, 21, 0)['shift']['id'] == 'REC-2300'
    assert m(RECEPTION, 1, 1)['status'] == 'unmatched'
    assert m(RECEPTION, 0, 30, day=2)['shift']['id'] == m(RECEPTION, 0, 30, day=1)['shift']['id']


def test_12h_pair_matches_each_start_and_dead_zones_between():
    pair = [pat('SEC-0800-2000', '08:00', '20:00'), pat('SEC-2000-0800', '20:00', '08:00')]
    assert m(pair, 8, 0)['shift']['id'] == 'SEC-0800-2000'
    assert m(pair, 20, 0)['shift']['id'] == 'SEC-2000-0800'
    assert m(pair, 6, 0)['shift']['id'] == 'SEC-0800-2000'
    assert m(pair, 5, 59)['status'] == 'unmatched'
    assert m(pair, 10, 0)['shift']['id'] == 'SEC-0800-2000'
    assert m(pair, 10, 1)['status'] == 'unmatched'
    assert m(pair, 22, 0)['shift']['id'] == 'SEC-2000-0800'
    assert m(pair, 0, 0)['status'] == 'unmatched'


def test_8h_and_12h_sharing_a_start_time_is_ambiguous_not_chosen_by_length():
    for h, mi in ((8, 0), (7, 0), (9, 30), (6, 0), (10, 0)):
        r = m(SECURITY, h, mi)
        assert r['status'] == 'ambiguous' and r['shift'] is None, (h, mi)
        assert r['reason'] == 'multiple_shifts_within_matching_window'
    # Same-start pair cannot be told apart by clock-in alone, at any margin.
    assert m(SECURITY, 20, 0)['shift']['id'] == 'SEC-2000-0800'
    assert m(SECURITY, 16, 0)['shift']['id'] == 'SEC-1600-0000'
    assert m(SECURITY, 0, 0)['shift']['id'] == 'SEC-0000-0800'


def test_duplicate_reason_only_when_single_match_among_duplicate_starts():
    # One of two same-start patterns has no margins: only one can match, and
    # the duplicate-start rule still refuses to choose.
    p = [pat('A12', '08:00', '20:00'), dict(pat('B8', '08:00', '16:00'), match_before_minutes=None, match_after_minutes=None)]
    r = m(p, 8, 0)
    assert r['status'] == 'ambiguous' and r['reason'] == 'duplicate_or_indistinguishable_shift_start_times'


def test_security_dead_zone_between_08_and_16_starts_uses_midpoint_exclusion():
    # Distinct starts 00,08,16,20 -> 08/16 gap 480 (cap 120), 16/20 gap 240 (119 each side).
    assert m(SECURITY, 17, 59)['shift']['id'] == 'SEC-1600-0000'
    assert m(SECURITY, 18, 0)['status'] == 'unmatched'          # midpoint minute of the 16/20 gap
    assert m(SECURITY, 18, 1)['shift']['id'] == 'SEC-2000-0800'
    assert m(SECURITY, 12, 0)['status'] == 'unmatched'


def test_missing_or_partial_inputs():
    assert match_shift_by_clock(at(8), [])['reason'] == 'no_active_shift_patterns'
    unconfigured = [dict(pat('X', '08:00', '16:00'), match_before_minutes=None, match_after_minutes=None)]
    assert match_shift_by_clock(at(8), unconfigured)['status'] == 'unmatched'
    # An unconfigured neighbour still clips a configured shift's window.
    p = [pat('REC', '07:00', '15:00'), dict(pat('NEAR', '09:00', '17:00'), match_before_minutes=None, match_after_minutes=None)]
    assert m(p, 7, 59)['shift']['id'] == 'REC'
    assert m(p, 8, 0)['status'] == 'unmatched'
    with pytest.raises(ValueError):
        match_shift_by_clock(at(8), [pat('BAD', '08:00', '16:00', before=121)])


def test_matcher_uses_clock_in_only_never_the_shift_length():
    long_ = [pat('L', '08:00', '20:00')]
    short = [pat('S', '08:00', '16:00')]
    assert m(long_, 8)['status'] == m(short, 8)['status'] == 'matched'
    # A punch inside the 8h end but outside the margin is unmatched: end_time is never read.
    assert m(long_, 14)['status'] == m(short, 14)['status'] == 'unmatched'


# ---------------------------------------------------------------- work date / reconciliation (pure)

def test_work_date_for_punch_12h_boundary_is_exclusive():
    # Gap larger than 12h (720) moves the date; exactly 720 does not.
    assert work_date_for_punch(datetime(2026, 11, 2, 8, 0), time(20, 0)) == date(2026, 11, 2)   # gap 720
    assert work_date_for_punch(datetime(2026, 11, 2, 7, 59), time(20, 0)) == date(2026, 11, 1)  # gap 721
    assert work_date_for_punch(datetime(2026, 11, 1, 20, 0), time(8, 0)) == date(2026, 11, 1)   # gap 720
    assert work_date_for_punch(datetime(2026, 11, 1, 20, 1), time(8, 0)) == date(2026, 11, 2)   # gap 721
    assert work_date_for_punch(datetime(2026, 11, 2, 0, 30), time(23, 0)) == date(2026, 11, 1)  # late-night shift
    assert work_date_for_punch(datetime(2026, 10, 31, 23, 30), time(0, 0)) == date(2026, 11, 1)  # early punch, 00:00 shift


def test_infer_non_security_shift_needs_review_when_unmatched_or_ambiguous():
    ok = infer_non_security_shift(at(0, 30, day=2), RECEPTION)
    assert ok['status'] == 'ready_for_review' and ok['shift']['id'] == 'REC-2300' and ok['work_date'] == D
    dead = infer_non_security_shift(at(11), RECEPTION)
    assert dead['status'] == 'needs_review' and dead['shift'] is None and dead['work_date'] is None
    amb = infer_non_security_shift(at(8), SECURITY)
    assert amb['status'] == 'needs_review' and amb['reason'] == 'multiple_shifts_within_matching_window'


def test_shift_window_overnight_and_midnight_end_roll_to_next_day():
    assert shift_window(D, SECURITY[1]) == (at(20), at(8, day=2))                 # 12h overnight
    assert shift_window(D, SECURITY[3]) == (at(16), at(0, day=2))                 # 16:00-00:00 ends next midnight
    assert shift_window(D, SECURITY[0]) == (at(8), at(20))                        # 12h same day


def rec(pattern, program_in=None, program_out=None, punches=()):
    return reconcile_attendance_day(work_date=D, shift_pattern=pattern, program_clock_in=program_in,
                                    program_clock_out=program_out, fingerprint_punches=list(punches))


def test_reconcile_8h_two_punches_early_and_late():
    r = rec(RECEPTION[0], punches=[at(6, 55), at(15, 10)])
    assert r['selected_clock_in'] == at(6, 55) and r['selected_clock_out'] == at(15, 10)
    assert r['worked_minutes'] == 8 * 60 + 15 and r['status'] == 'ready_for_review' and r['issues'] == []
    # Evidence window is scheduled -120 .. +120 inclusive. 04:59 and 17:01 are dropped silently.
    r = rec(RECEPTION[0], punches=[at(4, 59), at(5, 0), at(17, 0), at(17, 1)])
    assert r['first_clock_in'] == at(5, 0) and r['last_clock_out'] == at(17, 0)


def test_reconcile_12h_overnight_second_punch_next_morning():
    r = rec(SECURITY[1], punches=[at(19, 50), at(8, 5, day=2)])
    assert r['selected_clock_in'] == at(19, 50) and r['selected_clock_out'] == at(8, 5, day=2)
    assert r['worked_minutes'] == 12 * 60 + 15 and r['issues'] == []
    # The window ends at 10:00 next day; a punch at 10:01 is not evidence for this shift.
    r = rec(SECURITY[1], punches=[at(20), at(10, 1, day=2)])
    assert r['selected_clock_out'] is None and 'only_one_fingerprint_event' in r['issues']


def test_reconcile_missing_and_single_punch_never_invent_the_other_end():
    none = rec(RECEPTION[0])
    assert none['status'] == 'needs_review' and 'missing_fingerprint_evidence' in none['issues']
    assert 'incomplete_attendance_endpoints' in none['issues'] and none['worked_minutes'] is None
    one = rec(RECEPTION[0], punches=[at(7)])
    assert one['selected_clock_in'] == at(7) and one['selected_clock_out'] is None
    assert {'only_one_fingerprint_event', 'incomplete_attendance_endpoints'} <= set(one['issues'])
    # Program clock-out supplied: a lone punch is then used for the start only.
    both = rec(RECEPTION[0], program_out=at(15), punches=[at(7)])
    assert both['selected_clock_in'] == at(7) and both['selected_clock_out'] == at(15) and both['worked_minutes'] == 480


def test_reconcile_three_punches_takes_first_and_last_and_flags_nothing():
    r = rec(RECEPTION[0], punches=[at(7), at(11), at(15)])
    assert r['selected_clock_in'] == at(7) and r['selected_clock_out'] == at(15) and r['issues'] == []


def test_two_identical_punches_flag_duplicate_and_still_select_both_ends():
    r = rec(RECEPTION[0], punches=[at(7), at(7)])
    assert 'duplicate_fingerprint_events' in r['issues'] and r['selected_clock_out'] == at(7)
    assert 'clock_out_not_after_clock_in' in r['issues']


def test_day_with_two_shift_types_shares_the_boundary_punch_between_them():
    punches = [at(7), at(15), at(23)]
    morning = rec(RECEPTION[0], punches=punches)
    evening = rec(RECEPTION[1], punches=punches)
    assert (morning['selected_clock_in'], morning['selected_clock_out']) == (at(7), at(15))
    assert (evening['selected_clock_in'], evening['selected_clock_out']) == (at(15), at(23))
    # The 15:00 punch is the OUT of one shift and the IN of the other: no exclusivity or flag.
    assert morning['issues'] == [] and evening['issues'] == []
    # 8h + 12h on the same date: all punches fall in both windows and are not split.
    mixed = rec(SECURITY[0], punches=[at(8), at(16), at(20)])
    assert mixed['selected_clock_out'] == at(20)
    short = rec(SECURITY[2], punches=[at(8), at(16), at(20)])
    assert short['selected_clock_out'] == at(18, 0) or short['selected_clock_out'] == at(16)


# ---------------------------------------------------------------- evidence preview (pure)

def evidence(*times, issues=()):
    raw = b'synthetic-only'
    return FingerprintEvidence(sha256(raw).hexdigest(), raw, date(2026, 11, 1), date(2026, 11, 30),
        tuple(dict(employee_id=1, punch_at=t, sheet='Raw', row=i + 2) for i, t in enumerate(times)), issues)


def win(key, day, start, end, before=0, after=0, endday=None):
    s = datetime.combine(date(2026, 11, day), time.fromisoformat(start))
    e = datetime.combine(date(2026, 11, endday or day), time.fromisoformat(end))
    if e <= s:
        e += timedelta(days=1)
    return ExplicitShiftWindow(key, 1, date(2026, 11, day), s, e, before, after, 'Synthetic explicit schedule')


def prev(source, *windows):
    return preview_shift_occurrences(employee_id=1, work_date=D, windows=windows, fingerprint=source)


def test_preview_8h_window_edges_inclusive_with_margins():
    w = win('d', 1, '07:00', '15:00', 120, 120)
    r = prev(evidence(at(4, 59), at(5), at(17), at(17, 1)), w)
    assert [e['raw_event']['punch_at'] for e in r.occurrences] == [at(5), at(17)]
    assert r.diagnostic_counts['selected_outside_windows'] == 2
    assert all(e['direction'] is None for e in r.occurrences)


def test_preview_12h_overnight_window_spans_midnight_and_keeps_every_punch():
    w = win('n', 1, '20:00', '08:00')
    r = prev(evidence(at(19, 59), at(20), at(23, 59), at(0, 0, 2), at(8, 0, 2), at(8, 1, 2)), w)
    got = [e['raw_event']['punch_at'] for e in r.occurrences]
    assert got == [at(20), at(23, 59), at(0, 0, 2), at(8, 0, 2)]


def test_preview_two_punches_one_punch_none_no_direction_and_no_absence_claim():
    w = win('d', 1, '08:00', '20:00')
    two = prev(evidence(at(8), at(20)), w)
    assert len(two.occurrences) == 2 and not two.establishes_direction and not two.eligible_for_registration
    assert 'direction_and_completeness_unresolved' in two.issues
    one = prev(evidence(at(8)), w)
    assert len(one.occurrences) == 1 and one.occurrences[0]['direction'] is None
    none = prev(evidence(), w)
    assert not none.occurrences and 'no_occurrences_in_supplied_windows_not_absence' in none.issues


def test_preview_two_shift_types_same_day_touching_windows_overlap_and_share_punch():
    morning, evening = win('m', 1, '07:00', '15:00'), win('e', 1, '15:00', '23:00')
    r = prev(evidence(at(15)), morning, evening)
    assert r.occurrences[0]['candidate_window_ids'] == ('m', 'e')
    assert 'supplied_schedule_windows_overlap' in r.issues and 'occurrence_shared_between_windows' in r.issues
    # A one-minute gap with zero margins removes the overlap flag.
    spaced = win('e', 1, '15:01', '23:00')
    r2 = prev(evidence(at(15)), morning, spaced)
    assert 'supplied_schedule_windows_overlap' not in r2.issues


def test_preview_does_not_clip_margins_unlike_the_matcher():
    # Matcher clips 120/120 at 07:00 and 09:00 to non-overlap. Preview takes margins as given.
    a, b = win('a', 1, '07:00', '15:00', 120, 120), win('b', 1, '09:00', '17:00', 120, 120)
    r = prev(evidence(at(8)), a, b)
    assert r.occurrences[0]['candidate_window_ids'] == ('a', 'b') and 'supplied_schedule_windows_overlap' in r.issues
    # Matcher: gap 120 -> effective margins 59, so 08:00 (the midpoint) matches neither.
    assert match_shift_by_clock(at(8), [pat('a', '07:00', '15:00'), pat('b', '09:00', '17:00')])['status'] == 'unmatched'
    assert match_shift_by_clock(at(7, 59), [pat('a', '07:00', '15:00'), pat('b', '09:00', '17:00')])['shift']['id'] == 'a'


def test_preview_overnight_exit_shared_with_next_day_start_stays_unresolved():
    night, nxt = win('n', 1, '20:00', '08:00'), win('d', 2, '08:00', '20:00')
    r = prev(evidence(at(8, 0, 2)), night, nxt)
    assert r.occurrences[0]['candidate_window_ids'] == ('n', 'd') and 'occurrence_shared_between_windows' in r.issues


def test_preview_refuses_window_longer_than_a_day_and_bad_margins():
    with pytest.raises(ValueError):
        prev(evidence(at(8)), replace(win('x', 1, '08:00', '20:00'), scheduled_end=at(8, 1, 2)))
    with pytest.raises(ValueError):
        prev(evidence(at(8)), win('x', 1, '08:00', '20:00', 121, 0))


# ---------------------------------------------------------------- does shift choice reach the screen?

def _app(name):
    return (ROOT / 'app' / name).read_text(encoding='utf-8')


def _imported_modules(src):
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    return mods


def test_matcher_is_imported_only_by_legacy_reconciliation_not_by_the_shift_screen():
    users = sorted(p.name for p in (ROOT / 'app').glob('*.py')
                   if any(x.endswith('shift_matching') for x in _imported_modules(p.read_text(encoding='utf-8'))))
    assert users == ['attendance_reconciliation.py']
    for name in ('shift_day_routes.py', 'shift_evidence_source.py', 'shift_evidence_preview.py', 'shift_day_store.py'):
        assert 'match_shift_by_clock' not in _app(name)
    assert 'match_shift_by_clock' not in (ROOT / 'app/templates/shift_day_review.html').read_text(encoding='utf-8')


def test_screen_offers_every_active_department_pattern_and_user_picks_explicitly():
    routes = _app('shift_day_routes.py')
    # Choices are all ACTIVE patterns of the employee's CURRENT department, ordered by code, no 8h/12h filter.
    assert 'ShiftPattern.department_id==employee.department_id' in routes
    assert 'ShiftPattern.active.is_(True)' in routes and 'order_by(ShiftPattern.code)' in routes
    assert 'work_date' in routes and 'windows' in routes
    html = (ROOT / 'app/templates/shift_day_review.html').read_text(encoding='utf-8')
    assert 'for pattern in patterns' in html and 'type="checkbox" name="windows"' in html
    assert re.search(r'pattern\.start_time\s*}}\s*→\s*{{\s*pattern\.end_time', html)
    assert 'match_before_minutes' in html   # margins are shown, hours of the shift are shown, length is not computed


def test_screen_source_never_uses_shift_length_or_type():
    for name in ('shift_day_routes.py', 'shift_evidence_source.py'):
        src = _app(name)
        assert not re.search(r'daily_hours|shift_type|is_12h|12h', src)


# ---------------------------------------------------------------- DB-bound (UNRAN without PostgreSQL at head)

def _pg_ready():
    try:
        from sqlalchemy import text
        from app.db import engine
        if engine.dialect.name != 'postgresql':
            return False
        with engine.connect() as c:
            c.execute(text('SELECT 1'))
        return True
    except Exception:
        return False


pg = pytest.mark.skipif(not _pg_ready(), reason='PostgreSQL not available: UNRAN')

if _pg_ready():  # fixtures only importable when a database exists
    from tests.test_shift_evidence_source import setup_source  # noqa: F401
    from tests.test_leave_historical_evidence import history_context  # noqa: F401
    from tests.test_department_workbook_store import context  # noqa: F401


@pg
def test_source_8h_and_12h_patterns_both_selectable_by_id_in_one_department(history_context):  # noqa: F811
    from app.models import ShiftPattern
    from app.shift_evidence_source import preview_configured_shift_evidence
    db, auth, employee, pattern, args = setup_source(history_context)
    p12 = ShiftPattern(department_id=employee.department_id, code='SYN-12', name='Synthetic 12h', start_time=time(8), end_time=time(20), match_before_minutes=0, match_after_minutes=0, active=True)
    p8 = ShiftPattern(department_id=employee.department_id, code='SYN-8', name='Synthetic 8h', start_time=time(8), end_time=time(16), match_before_minutes=0, match_after_minutes=0, active=True)
    db.add_all([p12, p8]); db.flush()
    args['selected_windows'] = ((D, p12.id), (D, p8.id))
    args['fingerprint_bytes'] = b'ID,Date,Time\nSYN-HISTORY,2026-11-01,08:00 16:00 20:00\n'
    result = preview_configured_shift_evidence(db, **args)
    ids = {o['ordinal']: o['candidate_window_ids'] for o in result.preview.occurrences}
    assert ids[0] == (f'{D}:{p12.id}', f'{D}:{p8.id}')            # 08:00 in both windows
    assert 'supplied_schedule_windows_overlap' in result.preview.issues
    assert not result.posting_allowed


@pg
def test_source_refuses_pattern_of_other_department_or_inactive(history_context):  # noqa: F811
    from app.models import Department
    from app.shift_evidence_source import preview_configured_shift_evidence
    db, auth, employee, pattern, args = setup_source(history_context)
    other = Department(name='Synthetic other 8h12h', active=True); db.add(other); db.flush()
    pattern.department_id = other.id; db.flush()
    with pytest.raises(ValueError):
        preview_configured_shift_evidence(db, **args)


@pg
def test_source_overnight_pattern_window_ends_next_day_and_midnight_end_rolls(history_context):  # noqa: F811
    from app.shift_evidence_source import preview_configured_shift_evidence
    db, auth, employee, pattern, args = setup_source(history_context)
    result = preview_configured_shift_evidence(db, **args)
    w = result.preview.windows[0]
    assert w['scheduled_start'] == '2026-11-01T23:00:00' and w['scheduled_end'] == '2026-11-02T07:00:00'
    assert w['elapsed_minutes'] == 480
