from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DataError, IntegrityError

from app.db import SessionLocal
from app.models import Department, Employee, LeaveHoliday, LeaveMonthNote, LeaveRegisterEntry


@pytest.fixture
def migrated_session():
    # Real migrated PostgreSQL, transaction rolled back after every synthetic case.
    with SessionLocal() as db:
        connection = db.connection()
        transaction = connection.begin_nested()
        department = Department(name='Synthetic register foundation', active=True)
        db.add(department)
        db.flush()
        employee = Employee(code='SYN-REG-FOUNDATION', full_name='Synthetic only',
            department_id=department.id, position='Synthetic', hire_date=date(2026, 1, 1),
            status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
        db.add(employee)
        db.flush()
        yield db, employee
        transaction.rollback()
        db.rollback()


def entry(employee, **changes):
    values = {'employee_id': employee.id, 'work_date': date(2026, 1, 1), 'code': None,
                  'source': 'synthetic_import', 'recorded_by': 'synthetic_operator'}
    return LeaveRegisterEntry(**(values | changes))


def test_unknown_blank_is_not_explicit_present(migrated_session):
    db, employee = migrated_session
    unknown = entry(employee)
    present = entry(employee, work_date=date(2026, 1, 2), code='', source='explicit_review')
    db.add_all([unknown, present])
    db.flush()
    db.expire_all()
    rows = db.scalars(select(LeaveRegisterEntry).where(
        LeaveRegisterEntry.employee_id == employee.id).order_by(LeaveRegisterEntry.work_date)).all()
    assert [row.code for row in rows] == [None, '']
    assert employee.birth_date is None
    assert employee.prior_insurance_years is None


@pytest.mark.parametrize('code', ['p', 'x', 'a', 'AA'])
def test_database_accepts_only_normalized_codes(migrated_session, code):
    db, employee = migrated_session
    with pytest.raises((IntegrityError, DataError)), db.begin_nested():
        db.add(entry(employee, code=code))
        db.flush()


def test_database_unique_employee_date(migrated_session):
    db, employee = migrated_session
    db.add(entry(employee, code='W'))
    db.flush()
    with pytest.raises((IntegrityError, DataError)), db.begin_nested():
        db.add(entry(employee, code='S'))
        db.flush()


def test_holidays_notes_do_not_post_balances(migrated_session):
    db, employee = migrated_session
    before = db.execute(text('SELECT count(*) FROM leave_balance_transactions')).scalar_one()
    db.add(LeaveHoliday(holiday_date=date(2026, 1, 7), name='Synthetic holiday', created_by='synthetic'))
    db.add(LeaveMonthNote(employee_id=employee.id, year=2026, month=1,
                         notes='Synthetic missing fingerprint note', recorded_by='synthetic'))
    db.flush()
    assert db.execute(text('SELECT count(*) FROM leave_balance_transactions')).scalar_one() == before


def test_read_summary_preserves_unknowns_and_service_end(migrated_session):
    from app.leave_read_model import register_evidence_summary
    from app.models import EmployeeServicePeriod
    db, employee = migrated_session
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2026, 1, 1),
                                 end_date=date(2026, 1, 4), start_reason='Synthetic'))
    db.add_all([entry(employee, code='W'),
        entry(employee, code=None, work_date=date(2026, 1, 2)),
        entry(employee, code='', work_date=date(2026, 1, 3)),
        entry(employee, code='U', work_date=date(2026, 1, 5))])
    db.flush()
    summary = register_evidence_summary(db, employee.id, date(2026, 1, 1), date(2026, 1, 31))
    assert summary.employed_days == 4
    assert summary.unknown_days == 2
    assert summary.present_days == 1
    assert summary.codes['W'] == 1
    assert summary.codes['U'] == 0
    assert summary.conflicting_dates == (date(2026, 1, 5),)
    assert not db.new and not db.dirty and not db.deleted


def test_read_summary_includes_only_employee_period_month_notes(migrated_session):
    from app.leave_read_model import register_evidence_summary
    db, employee = migrated_session
    db.add_all([
        LeaveMonthNote(employee_id=employee.id, year=2026, month=1, notes='January note', recorded_by='synthetic'),
        LeaveMonthNote(employee_id=employee.id, year=2026, month=2, notes='February outside period', recorded_by='synthetic')])
    db.flush()
    summary = register_evidence_summary(db, employee.id, date(2026, 1, 1), date(2026, 1, 31))
    assert summary.notes == ('January note',)
    assert summary.employed_days == summary.present_days == 0
    assert sum(summary.codes.values()) == 0
    assert not db.new and not db.dirty and not db.deleted


def test_month_close_requires_host_backup_reference(migrated_session):
    from datetime import UTC, datetime

    from app.models import LeaveMonthClose
    db, _ = migrated_session
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add(LeaveMonthClose(year=2026, month=1, backup_run_id=-999999,
            closed_by='synthetic', closed_at=datetime.now(UTC), policy_snapshot_json='{}'))
        db.flush()


def test_close_metadata_does_not_auto_close_or_choose_policy(migrated_session):
    from app.models import LeaveMonthClose
    db, _ = migrated_session
    assert db.scalar(select(LeaveMonthClose).where(LeaveMonthClose.year == 2026,
                                                  LeaveMonthClose.month == 1)) is None
    assert not db.new and not db.dirty and not db.deleted


def test_missing_service_evidence_is_visible_not_silent_zero_entitlement(migrated_session):
    from app.leave_read_model import register_evidence_summary
    db, employee = migrated_session
    summary = register_evidence_summary(db, employee.id, date(2026, 1, 1), date(2026, 1, 31))
    assert summary.service_evidence_missing
    with pytest.raises(ValueError, match='does not exist'):
        register_evidence_summary(db, -999999, date(2026, 1, 1), date(2026, 1, 31))


def test_read_summary_does_not_flush_unrelated_pending_changes(migrated_session):
    from app.leave_read_model import register_evidence_summary
    db, employee = migrated_session
    pending = LeaveHoliday(holiday_date=date(2026, 7, 23), name='Synthetic pending',
                           created_by='synthetic')
    db.add(pending)
    original_salary = employee.basic_salary
    employee.basic_salary = Decimal('999.99')
    register_evidence_summary(db, employee.id, date(2026, 1, 1), date(2026, 1, 31))
    assert pending.id is None
    assert pending in db.new
    assert employee in db.dirty
    assert db.execute(text('SELECT basic_salary FROM employees WHERE id=:id'),
                      {'id': employee.id}).scalar_one() == original_salary
