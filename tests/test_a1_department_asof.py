"""A1 reproduction: current department must not be applied to a work date before the employee joined it."""
from datetime import date
import pytest
from app.models import *
from app.attendance_evidence_review import prepare_evidence_review
from tests.test_attendance_evidence_review import review_context  # noqa: F401
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def _joined_current_department_on(db, employee_id, effective):
    employee = db.get(Employee, employee_id)
    previous = Department(name='SYN-A1-PREVIOUS-DEPT')
    db.add(previous); db.flush()
    db.add(EmployeeChange(employee_id=employee_id, field_name='department_id', old_value=str(previous.id),
                          new_value=str(employee.department_id), effective_date=effective, reason='synthetic transfer'))
    db.flush()


def test_work_date_before_joining_current_department_is_refused(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    _joined_current_department_on(db, args['employee_id'], date(2026, 11, 10))
    with pytest.raises(ValueError):
        prepare_evidence_review(db, **args)   # work_date 2026-11-01 precedes the 2026-11-10 move


def test_work_date_on_or_after_joining_current_department_is_accepted(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    _joined_current_department_on(db, args['employee_id'], date(2026, 11, 1))
    assert prepare_evidence_review(db, **args).employee_id == args['employee_id']


def _chg(db, employee_id, old, new, effective):
    db.add(EmployeeChange(employee_id=employee_id, field_name='department_id', old_value=old, new_value=new,
                          effective_date=effective, reason='synthetic'))
    db.flush()


def _refused(db, args, text):
    with pytest.raises(ValueError, match=text):
        prepare_evidence_review(db, **args)


def test_future_chain_a_to_b_to_c_refused(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    _chg(db, args['employee_id'], '991', '992', date(2026, 11, 10))
    _chg(db, args['employee_id'], '992', cur, date(2026, 11, 20))
    _refused(db, args, 'changed after this workday')


def test_future_reversion_a_to_b_to_a_refused_conservatively(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    _chg(db, args['employee_id'], cur, '992', date(2026, 11, 10))
    _chg(db, args['employee_id'], '992', cur, date(2026, 11, 20))
    _refused(db, args, 'changed after this workday')


def test_equal_value_noop_row_is_not_a_block(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    _chg(db, args['employee_id'], cur, cur, date(2026, 11, 10))
    assert prepare_evidence_review(db, **args).employee_id == args['employee_id']


def test_same_day_conflicting_moves_refused(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    _chg(db, args['employee_id'], '991', '992', date(2026, 11, 1))
    _chg(db, args['employee_id'], '992', cur, date(2026, 11, 1))
    _refused(db, args, 'ambiguous')


def test_same_day_move_into_other_than_current_refused(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    _chg(db, args['employee_id'], '991', '992', date(2026, 11, 1))
    _refused(db, args, 'ambiguous')


@pytest.mark.parametrize('old,new', [(None, '5'), ('5', None), ('abc', '5'), ('5', '-1'), ('0', '5'), ('١٢', '5'), ('5 ', '6')])
def test_malformed_values_fail_closed(review_context, old, new):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    _chg(db, args['employee_id'], old, new, date(2026, 11, 10))
    _refused(db, args, 'malformed')


def test_previous_day_window_before_same_day_move_refused(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    _chg(db, args['employee_id'], '991', cur, date(2026, 11, 1))
    args['windows'] = ((date(2026, 10, 31), pattern.id), (date(2026, 11, 1), pattern.id))
    _refused(db, args, 'changed after this workday')


def test_earlier_than_workday_history_is_not_consulted(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    _chg(db, args['employee_id'], 'abc', None, date(2026, 10, 1))
    assert prepare_evidence_review(db, **args).employee_id == args['employee_id']


def test_preview_holds_share_lock_on_employee_changes_and_employees(review_context):  # noqa: F811
    """Real PG: a concurrent writer of employee_changes/employees must wait for this transaction."""
    from sqlalchemy import text
    db, actor, auth, source, export, receipt, pattern, args = review_context
    prepare_evidence_review(db, **args)
    modes = {r[0] for r in db.execute(text(
        "select c.relname||':'||l.mode from pg_locks l join pg_class c on c.oid=l.relation "
        "where l.pid=pg_backend_pid() and c.relname in ('employee_changes','employees') and l.granted")).all()}
    assert 'employee_changes:ShareLock' in modes and 'employees:ShareLock' in modes


def test_department_move_in_source_digest(review_context):  # noqa: F811
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    db.add(EmployeeChange(employee_id=args['employee_id'], field_name='department_id', old_value='991', new_value=cur,
                          effective_date=date(2026, 11, 1), reason='synthetic'))
    db.flush()
    row = prepare_evidence_review(db, **args)
    assert '"department_moves"' not in row.evidence_json  # facts live in the configured-source digest, not payload
    first = row.source_digest
    db.add(EmployeeChange(employee_id=args['employee_id'], field_name='department_id', old_value=cur, new_value=cur,
                          effective_date=date(2026, 11, 1), reason='noop'))
    db.flush()
    assert prepare_evidence_review(db, **args).source_digest == first  # equal-value no-op is not a fact change


def test_orm_transfer_flush_orders_employees_before_employee_changes(review_context):  # noqa: F811
    """Lock-order invariant: the transfer writer touches employees before employee_changes,
    the same order as the preview's single LOCK TABLE statement, so no cycle is added."""
    from sqlalchemy import event
    db, actor, auth, source, export, receipt, pattern, args = review_context
    employee = db.get(Employee, args['employee_id'])
    other = Department(name='SYN-A1-ORDER-DEPT'); db.add(other); db.flush()
    seen = []
    def capture(conn, cursor, statement, *a):
        s = statement.lstrip().upper()
        for table in ('EMPLOYEE_CHANGES', 'EMPLOYEES'):
            if (s.startswith('INSERT INTO ') or s.startswith('UPDATE ')) and (' ' + table + ' ') in (' ' + s.replace('(', ' ').replace(',', ' ') + ' ')[:80]:
                seen.append(table); break
    bind = db.get_bind()
    event.listen(bind, 'before_cursor_execute', capture)
    try:
        old = employee.department_id
        db.add(EmployeeChange(employee_id=employee.id, field_name='department_id', old_value=str(old),
                              new_value=str(other.id), effective_date=date(2026, 11, 10), reason='synthetic'))
        employee.department_id = other.id
        db.flush()
    finally:
        event.remove(bind, 'before_cursor_execute', capture)
    assert seen.index('EMPLOYEES') < seen.index('EMPLOYEE_CHANGES'), seen


def test_malformed_equal_noop_is_not_silently_ignored(review_context):
    db, actor, auth, source, export, receipt, pattern, args = review_context
    _chg(db, args['employee_id'], 'garbage', 'garbage', date(2026, 11, 1))
    _refused(db, args, 'malformed')


def test_representable_ten_digit_department_id_not_arbitrary_nine_digit_cap(review_context):
    db, actor, auth, source, export, receipt, pattern, args = review_context
    cur = str(db.get(Employee, args['employee_id']).department_id)
    _chg(db, args['employee_id'], '2147483647', cur, date(2026, 11, 1))
    assert prepare_evidence_review(db, **args).employee_id == args['employee_id']
