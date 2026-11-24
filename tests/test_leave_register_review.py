from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select

from app.leave_register_review import RegisterEdit, prepare_register_edit
from app.models import (
    EmployeeServicePeriod,
    LeaveBalanceTransaction,
    LeaveRecord,
    LeaveRegisterEntry,
    LeaveType,
)
from tests.test_leave_register_foundation import migrated_session  # noqa: F401


def edit_for(employee, **changes):
    return RegisterEdit(**({'employee_id': employee.id, 'work_date': date(2026, 1, 2),
        'code': None, 'certificate_attached': None, 'notes': None,
        'source': 'synthetic_explicit_review', 'period_start': date(2026, 1, 1),
        'period_end': date(2026, 1, 31)} | changes))


def service(db, employee):
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2026, 1, 1),
                                end_date=date(2026, 1, 3), start_reason='Synthetic'))
    db.flush()


def test_unknown_present_and_outside_service_are_distinct(migrated_session):  # noqa: F811
    db, employee = migrated_session
    assert prepare_register_edit(db, edit_for(employee)).issues == ('employment_evidence_missing',)
    service(db, employee)
    unknown = prepare_register_edit(db, edit_for(employee))
    present = prepare_register_edit(db, edit_for(employee, code=''))
    assert unknown.source_digest != present.source_digest
    assert unknown.proposed_code is None and present.proposed_code == ''
    assert unknown.issues == present.issues == ()
    assert prepare_register_edit(db, edit_for(employee, code='—')).issues == (
        'outside_service_code_conflicts_with_employment',)
    assert prepare_register_edit(db, edit_for(employee, work_date=date(2026, 1, 4), code='U')).issues == (
        'day_outside_verified_employment',)
    assert prepare_register_edit(db, edit_for(employee, work_date=date(2026, 1, 4), code='—')).issues == ()


def test_persisted_digest_revision_and_dirty_session_preserved(migrated_session):  # noqa: F811
    db, employee = migrated_session
    service(db, employee)
    row = LeaveRegisterEntry(employee_id=employee.id, work_date=date(2026, 1, 2),
        code='A', source='synthetic_explicit_review', recorded_by='synthetic', revision=1)
    db.add(row)
    db.flush()
    proposal = edit_for(employee, code='A')
    before = prepare_register_edit(db, proposal)
    assert before.unchanged and before.previous_revision == 1
    count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    employee.full_name = 'Synthetic pending name'
    row.code = 'U'
    after = prepare_register_edit(db, proposal)
    assert before == after
    assert row.code == 'U' and employee.full_name == 'Synthetic pending name'
    assert row in db.dirty and employee in db.dirty
    with db.no_autoflush:
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == count
    db.flush()
    assert prepare_register_edit(db, proposal).source_digest != before.source_digest


def test_confirmed_legacy_leave_requires_reconciliation_not_second_debit(migrated_session):  # noqa: F811
    db, employee = migrated_session
    service(db, employee)
    kind = LeaveType(code='synregreview', name_ar='Synthetic', name_en='Synthetic',
                     active=True, tracks_balance=True, is_paid=True)
    db.add(kind)
    db.flush()
    record = LeaveRecord(employee_id=employee.id, leave_type_id=kind.id,
        start_date=date(2026, 1, 2), end_date=date(2026, 1, 2), calendar_days=1,
        charged_days=1, reason='Synthetic', status='confirmed', created_by='synthetic')
    db.add(record)
    db.flush()
    result = prepare_register_edit(db, edit_for(employee, code='A'))
    assert result.issues == ('linked_leave_reconciliation_required',)
    assert not db.new and not db.dirty


@pytest.mark.parametrize('changes', [
    {'employee_id': True}, {'code': 'a'}, {'code': False},
    {'certificate_attached': 'false'}, {'source': ''}, {'source': ' source '},
    {'notes': 3}, {'period_start': date(2026, 1, 3)}, {'work_date': '2026-01-02'},
])
def test_invalid_preparation_never_queries_database(changes):
    proposal = RegisterEdit(1, date(2026, 1, 2), None, None, None, 'Synthetic',
                            date(2026, 1, 1), date(2026, 1, 31))
    with pytest.raises(ValueError):
        prepare_register_edit(None, replace(proposal, **changes))


def test_closed_month_anywhere_in_supplied_period_blocks_without_reopen_inference(migrated_session):  # noqa: F811
    from app.models import BackupRun, LeaveMonthClose, utcnow

    db, employee = migrated_session
    service(db, employee)
    backup = BackupRun(backup_type='manual', status='failed', size_bytes=0, initiated_by='synthetic')
    db.add(backup)
    db.flush()
    db.add(LeaveMonthClose(year=2025, month=12, backup_run_id=backup.id,
        closed_by='synthetic', closed_at=utcnow(), policy_snapshot_json='{}'))
    db.flush()
    # This does not validate or trust the backup, it refuses on closure alone.
    edit = edit_for(employee, period_start=date(2025, 12, 20))
    assert prepare_register_edit(db, edit).issues == ('source_period_contains_closed_month',)
    assert prepare_register_edit(db, edit_for(employee)).issues == ()


def test_conflicting_overlapping_service_facts_never_choose_first_period(migrated_session):  # noqa: F811
    db, employee = migrated_session
    service(db, employee)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=date(2026, 1, 2),
                                end_date=date(2026, 1, 4), start_reason='Synthetic conflict'))
    db.flush()
    assert prepare_register_edit(db, edit_for(employee)).issues == ('overlapping_employment_evidence',)
