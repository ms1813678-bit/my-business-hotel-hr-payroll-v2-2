"""Pack A item 1/A (base 0bc929d): characterization tests, NEW file only.

(1) DB: employee code / fingerprint_id uniqueness and compare_department_fingerprint
    alias-collision refusal with two employees BOTH in the exported department.
(2) CSV and legacy .xls: employee code change mid-month in one file.

Pins CURRENT behavior. A pass means "the code does this today", not "approved HR
rule". Gaps are named OWNER_QUESTION_* in docstrings. Synthetic data only. DB tests
need PostgreSQL at alembic head (repo conftest); every DB change is rolled back by
the shared fixtures. .xls tests write files with xlwt and are skipped (reported UNRAN,
not faked) when xlwt is not installed.
"""
import json
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.attendance_import import parse_attendance_workbook
from app.models import Department, Employee, EmployeeServicePeriod
from app.leave_manual_opening_groups import persist_manual_opening_group
from tests.test_attendance_import import workbook_bytes
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401

D1, D2 = date(2026, 11, 1), date(2026, 11, 20)
FP_HEAD = ['AC-No.', 'Name', 'Date', 'Check Time']


def _employee(db, department_id, code, fingerprint_id, name):
    e = Employee(code=code, fingerprint_id=fingerprint_id, full_name=name, department_id=department_id,
                 position='Synthetic', hire_date=date(2025, 1, 1), status='active',
                 basic_salary=Decimal(1), daily_hours=Decimal(8))
    db.add(e)
    db.flush()
    return e


# ------------------------------------------------------------------ DB uniqueness
def test_db_refuses_exact_duplicate_code_and_fingerprint_id_but_not_case_or_cross_column(history_context):  # noqa: F811
    """CURRENT: unique(code) and unique(fingerprint_id) are exact, case-sensitive and
    per column. So two employees can never share an identical code, but 'abc' vs 'ABC',
    and employee A's code == employee B's fingerprint_id, ARE accepted by the DB.
    OWNER_QUESTION_A1: should codes be unique case-insensitively and across both columns?"""
    db, _a, _auth, fact = history_context
    emp = db.get(Employee, fact.employee_id)
    other_dept = Department(name='Synthetic other dept', active=True)
    db.add(other_dept)
    db.flush()
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            _employee(db, other_dept.id, emp.code, None, 'dup code')
    with db.begin_nested():
        _employee(db, other_dept.id, 'FPX-1', 'fpx-one', 'first')
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            _employee(db, other_dept.id, 'FPX-2', 'fpx-one', 'dup fingerprint')
    with db.begin_nested():
        _employee(db, other_dept.id, 'fpx-1', None, 'case variant of code')      # accepted
        _employee(db, other_dept.id, 'FPX-3', emp.code, 'fp id == other code')   # accepted
    assert db.scalar(select(Employee.id).where(Employee.code == 'fpx-1')) is not None


# ------------------------------------------------------------------ DB workflow collision
def _two_employee_export(db, auth, employees):
    from app.department_contract import export_confirmed_department
    from app.leave_opening_source import confirm_opening_source, review_opening_source
    from app.leave_policy_preview import LeavePolicy
    from app.leave_policy_store import persist_policy_version
    policy = persist_policy_version(db, policy=LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90, 180)),
                                    reason='Synthetic explicit policy', **auth)
    selected = []
    for emp in employees:
        db.add(EmployeeServicePeriod(employee_id=emp.id, start_date=date(2025, 1, 1)))
        db.flush()
        group, _ = persist_manual_opening_group(db, employee_id=emp.id,
            balances={'A': Decimal(8), 'W': Decimal(2), 'H': Decimal(0)},
            effective_start=date(2026, 5, 17), reason='Synthetic source', **auth)
        review = review_opening_source(db, group_id=group.id, policy_id=policy.id, **auth)
        selected.append(confirm_opening_source(db, group_id=group.id, policy_id=policy.id,
            expected_previous_id=None, reviewed_source_sha256=review['source_sha256'],
            reason='Synthetic source confirmation', human_confirmed=True, **auth).id)
    months = tuple(date(2026, m, 1) for m in range(1, 6))
    return export_confirmed_department(db, selection_ids=tuple(selected), department_id=employees[0].department_id,
        month_start=date(2026, 5, 1), current_insurance_years={(e.id, m): None for e in employees for m in months},
        age_evaluation_dates={m: m for m in months}, **auth)


def _compare(db, export, auth, rows):
    from app.department_fingerprint_workflow import compare_department_fingerprint
    return compare_department_fingerprint(db, export_id=export.export_id, department_bytes=export.payload,
        fingerprint_bytes=workbook_bytes([('Att.log report', rows)]), fingerprint_filename='synthetic.xlsx', **auth)


def test_two_employees_in_exported_department_distinct_codes_bind_separately(history_context):  # noqa: F811
    """CURRENT (control): with two exported employees and no overlap, both code and
    fingerprint_id of each bind to that employee; alias_bindings lists all four aliases."""
    db, _a, auth, fact = history_context
    e1 = db.get(Employee, fact.employee_id)
    e2 = _employee(db, e1.department_id, 'SYN-SECOND', 'SYN-FP-2', 'Synthetic second')
    export = _two_employee_export(db, auth, [e1, e2])
    row = _compare(db, export, auth, [FP_HEAD, ['SYN-FP-2', 'x', date(2026, 5, 17), '08:00'],
                                       [e1.code, 'x', date(2026, 5, 17), '09:00']])
    evidence = json.loads(row.review_json)
    bound = {(a, i) for a, _n, i in evidence['alias_bindings']}
    assert (e1.code, e1.id) in bound and ('SYN-SECOND', e2.id) in bound and ('SYN-FP-2', e2.id) in bound
    by_emp = {d['employee_id']: d['punches'] for d in evidence['days'] if d['punches']}
    assert [p['raw_employee_code'] for p in by_emp[e2.id]] == ['SYN-FP-2']
    assert [p['raw_employee_code'] for p in by_emp[e1.id]] == [e1.code]


@pytest.mark.parametrize('variant', ['same_case', 'casefold'])
def test_two_exported_employees_sharing_an_alias_across_columns_are_refused(history_context, variant):  # noqa: F811
    """CURRENT: employee 2's fingerprint_id equals employee 1's code (exact or
    case-insensitive; DB allows both). Both are in the exported department, so no
    department can disambiguate: compare raises ValueError('...collision...') and
    writes nothing. OWNER_QUESTION_A2: refuse (today), or let the department/date decide?"""
    from app.models import DepartmentFingerprintPreparation
    db, _a, auth, fact = history_context
    e1 = db.get(Employee, fact.employee_id)
    shared = e1.code if variant == 'same_case' else e1.code.casefold()
    e2 = _employee(db, e1.department_id, 'SYN-SECOND', shared, 'Synthetic second')
    export = _two_employee_export(db, auth, [e1, e2])
    before = len(db.scalars(select(DepartmentFingerprintPreparation.id)).all())
    with pytest.raises(ValueError, match='collision'):
        _compare(db, export, auth, [FP_HEAD, [e1.code, 'x', date(2026, 5, 17), '08:00']])
    assert len(db.scalars(select(DepartmentFingerprintPreparation.id)).all()) == before


def test_cross_department_alias_overlap_with_only_one_in_export_is_also_refused_today(history_context):  # noqa: F811
    """CURRENT: owners are computed over ALL employees, not just the exported
    department, so an alias used by an employee of another department refuses the
    comparison even when that employee is not in the export (same behavior the
    repo's existing test shows). OWNER_QUESTION_A3: should unrelated departments block?"""
    db, _a, auth, fact = history_context
    e1 = db.get(Employee, fact.employee_id)
    other = Department(name='Synthetic other dept', active=True)
    db.add(other)
    db.flush()
    _employee(db, other.id, 'SYN-OUT', e1.code.casefold(), 'Synthetic outsider')
    export = _two_employee_export(db, auth, [e1])
    with pytest.raises(ValueError, match='collision'):
        _compare(db, export, auth, [FP_HEAD, [e1.code, 'x', date(2026, 5, 17), '08:00']])


# ------------------------------------------------------------------ mid-month code change
ALIASES = {'OLD-7': 1, 'NEW-7': 1, 'NEW-9': 2}


def _csv(rows):
    return ('\n'.join(','.join(str(c) for c in r) for r in rows) + '\n').encode()


CHANGE_ROWS = [FP_HEAD,
               ['OLD-7', 'Syn One', '2026-11-01', '08:00 16:00'],
               ['NEW-7', 'Syn One', '2026-11-20', '08:00 16:00'],
               ['OLD-7', 'Syn One', '2026-11-21', '08:00'],      # old code after the change date
               ['NEW-9', 'Syn Two', '2026-11-20', '09:00']]


def _check_mid_month(parsed):
    ev = parsed['events']
    assert [(e['raw_employee_code'], e['employee_id'], e['punch_at'].day) for e in ev] == [
        ('OLD-7', 1, 1), ('OLD-7', 1, 1), ('NEW-7', 1, 20), ('NEW-7', 1, 20),
        ('OLD-7', 1, 21), ('NEW-9', 2, 20)]
    assert parsed['issues'] == []
    assert parsed['period_start'] == date(2026, 11, 1) and parsed['period_end'] == date(2026, 11, 21)


def test_csv_code_change_mid_month_both_codes_resolve_with_no_effective_date_check():
    """CURRENT (CSV): both codes of employee 1 resolve for the whole file; an old code
    used AFTER the changeover date is accepted silently (no validity dates exist).
    OWNER_QUESTION_A4: how is the changeover date recorded, and is the old code
    after that date an error?"""
    _check_mid_month(parse_attendance_workbook(_csv(CHANGE_ROWS), 'x.csv', 'fingerprint', ALIASES))


def test_csv_code_change_with_only_new_code_known_marks_old_rows_unknown():
    """CURRENT (CSV): if only the new code is in the alias map, every old-code row is an
    unknown_employee (event kept, employee_id None); nothing carries the history over."""
    parsed = parse_attendance_workbook(_csv(CHANGE_ROWS), 'x.csv', 'fingerprint', {'NEW-7': 1, 'NEW-9': 2})
    old = [e for e in parsed['events'] if e['raw_employee_code'] == 'OLD-7']
    assert len(old) == 3 and all(e['employee_id'] is None for e in old)
    assert [i['reason'] for i in parsed['issues']].count('unknown_employee') == 2


def test_csv_same_punch_under_old_and_new_code_is_a_duplicate_punch():
    """CURRENT (CSV): same employee+instant under old and new code: second is
    duplicate_punch and dropped (legacy mode)."""
    rows = [FP_HEAD, ['OLD-7', 'x', '2026-11-20', '08:00'], ['NEW-7', 'x', '2026-11-20', '08:00']]
    parsed = parse_attendance_workbook(_csv(rows), 'x.csv', 'fingerprint', ALIASES)
    assert len(parsed['events']) == 1 and parsed['events'][0]['raw_employee_code'] == 'OLD-7'
    assert [i['reason'] for i in parsed['issues']] == ['duplicate_punch']


xlwt = pytest.importorskip('xlwt', reason='xlwt not installed: .xls tests UNRAN, not faked')


def _xls(rows):
    book = xlwt.Workbook()
    sheet = book.add_sheet('Att.log report')
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            sheet.write(r, c, value)
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def test_xls_code_change_mid_month_matches_csv_behavior():
    """CURRENT (.xls via xlrd): identical outcome to CSV when dates/times are text."""
    _check_mid_month(parse_attendance_workbook(_xls(CHANGE_ROWS), 'x.xls', 'fingerprint', ALIASES))


def test_xls_flat_table_numeric_code_arrives_as_float_text_and_misses_integer_alias():
    """CURRENT (.xls flat table layout): xlrd returns numeric ids as floats and
    normalize() makes '7.0', so numeric code 7 does NOT match alias '7' (all rows
    unknown_employee, value '7.0'); an alias spelled '7.0' does match. The float->int
    fix exists only in the day-matrix layout. A finding, not an assertion of intent.
    OWNER_QUESTION_A5: must numeric ids in legacy .xls match integer aliases in the
    flat layout like they do in the matrix layout? Which alias spelling is canonical?"""
    rows = [FP_HEAD, [7, 'x', '2026-11-01', '08:00'], [70, 'x', '2026-11-20', '08:00']]
    miss = parse_attendance_workbook(_xls(rows), 'x.xls', 'fingerprint', {'7': 1, '70': 1})
    assert [e['employee_id'] for e in miss['events']] == [None, None]
    assert [(i['reason'], i['value']) for i in miss['issues']] == [
        ('unknown_employee', '7.0'), ('unknown_employee', '70.0')]
    hit = parse_attendance_workbook(_xls(rows), 'x.xls', 'fingerprint', {'7.0': 1, '70.0': 1})
    assert [e['employee_id'] for e in hit['events']] == [1, 1]
