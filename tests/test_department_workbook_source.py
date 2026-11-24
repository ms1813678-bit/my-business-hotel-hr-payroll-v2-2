import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal, engine
from app.department_workbook_source import read_workbook_source
from app.models import Department, Employee, EmployeeServicePeriod, LeaveRegisterEntry
from app.restore_lock import read_restore_epoch


@pytest.fixture
def source_context():
    with SessionLocal() as db:
        try:
            dep = Department(name='Synthetic workbook source', active=True)
            db.add(dep); db.flush()
            employee = Employee(code='SYN-WB-SOURCE', full_name='Synthetic employee', department_id=dep.id,
                position='Synthetic', hire_date=date(2026, 1, 1), status='active',
                basic_salary=Decimal('12345.6789'), daily_hours=Decimal(8))
            db.add(employee); db.flush()
            args = {'department_id': dep.id, 'employee_ids': (employee.id,),
                    'window_start': date(2026, 1, 1), 'month_start': date(2026, 5, 1),
                    'policy_digest': 'a'*64, 'expected_epoch': read_restore_epoch(db)}
            yield db, employee, args
        finally:
            db.rollback()


def test_missing_service_is_not_inferred_from_hire_or_active(source_context):
    db, employee, args = source_context
    facts = read_workbook_source(db, **args)
    assert facts.missing_service_employee_ids == (employee.id,)
    assert facts.employed_dates == ((employee.id, frozenset()),)
    assert 'salary' not in facts.canonical_json
    assert '12345.6789' not in facts.canonical_json
    assert facts.digest == read_workbook_source(db, **args).digest


def test_exact_service_dates_revision_and_policy_change_digest(source_context):
    db, employee, args = source_context
    before = read_workbook_source(db, **args)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2026, 5, 3), end_date=date(2026, 5, 5)))
    db.flush()
    after = read_workbook_source(db, **args)
    assert after.digest != before.digest
    assert after.employed_dates == ((employee.id, frozenset(date(2026, 5, n) for n in (3, 4, 5))),)
    assert not after.missing_service_employee_ids
    entry = LeaveRegisterEntry(employee_id=employee.id, work_date=date(2026, 5, 3), code='W',
        source='synthetic', recorded_by='synthetic', revision=1)
    db.add(entry); db.flush()
    first = read_workbook_source(db, **args)
    entry.revision = 2; db.flush()
    second = read_workbook_source(db, **args)
    assert first.digest != second.digest
    assert second.digest != read_workbook_source(db, **{**args, 'policy_digest': 'b'*64}).digest
    assert json.loads(second.canonical_json)['register'][0]['revision'] == 2


def test_share_snapshot_blocks_concurrent_source_dml_until_transaction_ends(source_context):
    db, _employee, args = source_context
    read_workbook_source(db, **args)
    with engine.connect() as other, other.begin():
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError, match='lock timeout'):
            other.execute(text('LOCK TABLE leave_register_entries IN ROW EXCLUSIVE MODE'))
        other.rollback()
    db.rollback()
    with engine.begin() as other:
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        other.execute(text('LOCK TABLE leave_register_entries IN ROW EXCLUSIVE MODE'))


@pytest.mark.parametrize('change', [ {'employee_ids': (True,)}, {'employee_ids': ()},
    {'employee_ids': (1, 1)}, {'month_start': date(2026, 5, 2)}, {'policy_digest': 'invalid'}])
def test_explicit_selection_refuses_invalid_inputs(source_context, change):
    db, _employee, args = source_context
    with pytest.raises(ValueError):
        read_workbook_source(db, **{**args, **change})


def test_department_mismatch_refused(source_context):
    db, _employee, args = source_context
    with pytest.raises(ValueError):
        read_workbook_source(db, **{**args, 'department_id': 2147483647})


def test_retained_setting_object_does_not_make_epoch_read_stale():
    from app.models import SystemSetting
    key = 'restore_epoch'
    with engine.begin() as conn:
        old = conn.execute(text('SELECT value FROM system_settings WHERE key=:key'), {'key': key}).scalar_one_or_none()
        conn.execute(text("INSERT INTO system_settings(key,value,updated_by,updated_at) VALUES (:key,'source-test-E1','synthetic',now()) ON CONFLICT (key) DO UPDATE SET value='source-test-E1'"), {'key': key})
    try:
        with SessionLocal() as db:
            held = db.get(SystemSetting, key)
            assert held.value == 'source-test-E1'
            with engine.begin() as conn:
                conn.execute(text("UPDATE system_settings SET value='source-test-E2' WHERE key=:key"), {'key': key})
            assert held.value == 'source-test-E1'
            assert read_restore_epoch(db) == 'source-test-E2'
            from fastapi import HTTPException

            from app.restore_lock import acquire_credential_write_lock
            with pytest.raises(HTTPException) as refusal:
                acquire_credential_write_lock(db, expected_epoch='source-test-E1')
            assert refusal.value.status_code == 409
    finally:
        with engine.begin() as conn:
            if old is None:
                conn.execute(text('DELETE FROM system_settings WHERE key=:key'), {'key': key})
            else:
                conn.execute(text('UPDATE system_settings SET value=:value WHERE key=:key'), {'key': key, 'value': old})


def test_source_timeout_restores_caller_setting(source_context):
    db, _employee, args = source_context
    db.execute(text("SET LOCAL lock_timeout='7s'"))
    read_workbook_source(db, **args)
    assert db.scalar(text('SHOW lock_timeout')) == '7s'
