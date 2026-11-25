"""Real migrated PostgreSQL tests for the read-only carry availability service.

Synthetic data only. Fixtures reuse the existing prior_year chain: a recorded
weekly_rest carry of exactly 37/7 with window 2026-01-01..2026-04-30.
Pure-kernel tests at the end need no database writes and pin the window/sign
rules plus the consistent-recomputed-digest forgery resistance of the pure
evidence verifier.
"""
import json
from datetime import date
from fractions import Fraction

import pytest
from sqlalchemy import func, select, text

from app.leave_carry_allocation_store import record_carry_allocation, review_carry_allocation
from app.leave_carry_availability import (
    evaluate_carry_window,
    read_carry_availability,
    verify_allocation_evidence,
)
from app.leave_month_earning import canonical, digest
from app.models import AuditLog, LeaveBalanceTransaction, LeaveCarryAllocation
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_carry_allocation_store import call

OPERATION = 'e' * 64


def recorded(prior_year):
    db, employee, kinds, args = prior_year
    selection = call(args)
    review = review_carry_allocation(db, **selection)
    record_carry_allocation(db, operation_id=OPERATION, reviewed_digest=review['reviewed_digest'],
        human_confirmed=True, **selection)
    identity = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    return db, employee, kinds, identity


def read(db, employee, kinds, identity, **overrides):
    selection = dict(allocation_id=OPERATION, employee_id=employee.id, leave_type_id=kinds['W'],
        work_date=date(2026, 2, 1), **identity)
    selection.update(overrides)
    return read_carry_availability(db, **selection)


def counts(db):
    return (db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)),
        db.scalar(select(func.count()).select_from(LeaveCarryAllocation)),
        db.scalar(select(func.count()).select_from(AuditLog)))


@pytest.mark.parametrize('day,eligible', [
    (date(2026, 1, 1), True), (date(2026, 4, 30), True), (date(2026, 2, 15), True),
    (date(2025, 12, 31), False), (date(2026, 5, 1), False)])
def test_window_boundaries_inclusive_and_outside_preserved_unavailable(prior_year, day, eligible):
    db, employee, kinds, identity = recorded(prior_year)
    before = counts(db)
    result = read(db, employee, kinds, identity, work_date=day)
    assert result['eligible'] is eligible
    assert result['within_window'] is eligible
    assert result['source_amount'] == '37/7'
    assert result['available_grant_ceiling'] == ('37/7' if eligible else '0/1')
    assert result['preserved_unavailable'] == ('0/1' if eligible else '37/7')
    assert result['debt_amount'] == '0/1'
    assert result['source_year'] == 2025 and result['target_year'] == 2026
    assert result['available_from'] == '2026-01-01' and result['available_through'] == '2026-04-30'
    assert result['posting_allowed'] is False and result['operational_spending_allowed'] is False
    assert result['is_remaining_spendable'] is False and result['remaining_spendable'] is None
    assert result['consumption_provenance'] == 'unresolved'
    assert counts(db) == before and not db.new and not db.dirty and not db.deleted


def test_deterministic_output_and_no_writes(prior_year):
    db, employee, kinds, identity = recorded(prior_year)
    before = counts(db)
    first = read(db, employee, kinds, identity)
    assert first == read(db, employee, kinds, identity)
    assert counts(db) == before


def test_later_valid_debit_does_not_make_receipt_stale_or_spendable(prior_year):
    from decimal import Decimal
    db, employee, kinds, identity = recorded(prior_year)
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kinds['W'],
        transaction_type='leave_use', days_delta=Decimal(-2),
        reason='Synthetic later debit', created_by='synthetic'))
    db.flush()
    result = read(db, employee, kinds, identity)
    assert result['eligible'] and result['available_grant_ceiling'] == '37/7'
    assert result['is_remaining_spendable'] is False and result['remaining_spendable'] is None
    assert 'later_debit_consumption_attribution_not_recorded' in result['unresolved']


@pytest.mark.parametrize('change', ['employee', 'type', 'missing', 'malformed_id'])
def test_wrong_scope_or_missing_receipt_refuses(prior_year, change):
    db, employee, kinds, identity = recorded(prior_year)
    overrides = {}
    if change == 'employee':
        overrides['employee_id'] = employee.id + 1000
    elif change == 'type':
        overrides['leave_type_id'] = kinds['H']
    elif change == 'missing':
        overrides['allocation_id'] = 'f' * 64
    else:
        overrides['allocation_id'] = 'not-an-operation-id'
    with pytest.raises(ValueError):
        read(db, employee, kinds, identity, **overrides)


def test_reader_may_be_a_different_current_authorized_session(prior_year):
    from app.models import AdminAccount, AdminSession
    from app.restore_lock import read_restore_epoch
    db, employee, kinds, identity = recorded(prior_year)
    other = AdminAccount(username='synthetic-availability-reader', password_hash='not-a-real-login',
        role='system_admin', active=True)
    db.add(other); db.flush()
    session = AdminSession(id='synthetic-availability-reader-session', admin_account_id=other.id)
    db.add(session); db.flush()
    result = read(db, employee, kinds, dict(actor_id=other.id, session_id=session.id,
        session_version=other.session_version, expected_epoch=read_restore_epoch(db)))
    assert result['read_by_actor_id'] == other.id
    assert result['original_grant']['actor_id'] == identity['actor_id']
    assert result['original_grant']['session_id'] == identity['session_id']
    assert result['original_grant']['session_id'] != session.id


@pytest.mark.parametrize('change', ['role', 'epoch', 'version', 'session'])
def test_reader_authority_changes_refuse(prior_year, change):
    from app.models import AdminAccount, AdminSession
    db, employee, kinds, identity = recorded(prior_year)
    if change == 'role':
        db.get(AdminAccount, identity['actor_id']).role = 'finance'; db.flush()
    elif change == 'epoch':
        identity = {**identity, 'expected_epoch': 'different-restore-epoch'}
    elif change == 'version':
        identity = {**identity, 'session_version': identity['session_version'] + 1}
    else:
        session = db.get(AdminSession, identity['session_id']); db.delete(session); db.flush()
    with pytest.raises(ValueError):
        read(db, employee, kinds, identity)


def forge_allocation(db, sql, **params):
    db.execute(text('ALTER TABLE leave_carry_allocations DISABLE TRIGGER USER'))
    db.execute(text(sql), params)
    db.execute(text('ALTER TABLE leave_carry_allocations ENABLE TRIGGER USER'))
    db.expire_all()


@pytest.mark.parametrize('mode', ['result_only', 'result_digest_consistent', 'request_digest_consistent',
                                  'both_consistent_other_amount', 'target_year_consistent',
                                  'source_digest', 'columns', 'window_consistent'])
def test_forged_receipt_refuses_even_with_consistent_recomputed_digests(prior_year, mode):
    db, employee, kinds, identity = recorded(prior_year)
    row = db.execute(text('SELECT request_json,result_json FROM leave_carry_allocations')).one()
    request, result = json.loads(row.request_json), json.loads(row.result_json)
    if mode == 'result_only':
        forge_allocation(db, 'UPDATE leave_carry_allocations SET result_json=:r',
            r=canonical({**result, 'carried_amount': '99/1'}))
    elif mode == 'result_digest_consistent':
        forged = {**result, 'carried_amount': '99/1'}
        forge_allocation(db, 'UPDATE leave_carry_allocations SET result_json=:r, result_digest=:d',
            r=canonical(forged), d=digest(forged))
    elif mode == 'request_digest_consistent':
        forged = {**request, 'actor_id': 999}
        forge_allocation(db, 'UPDATE leave_carry_allocations SET request_json=:r, request_digest=:d',
            r=canonical(forged), d=digest(forged))
    elif mode == 'both_consistent_other_amount':
        forged_result = {**result, 'carried_amount': '99/1'}
        forged = {**request, 'plan': forged_result, 'reviewed_digest': digest(forged_result)}
        forge_allocation(db, 'UPDATE leave_carry_allocations SET request_json=:q, request_digest=:qd, '
            'result_json=:r, result_digest=:rd', q=canonical(forged), qd=digest(forged),
            r=canonical(forged_result), rd=digest(forged_result))
    elif mode == 'target_year_consistent':
        forged_result = {**result, 'target_year': 2030}
        forged = {**request, 'plan': forged_result, 'reviewed_digest': digest(forged_result)}
        forge_allocation(db, 'UPDATE leave_carry_allocations SET request_json=:q, request_digest=:qd, '
            'result_json=:r, result_digest=:rd', q=canonical(forged), qd=digest(forged),
            r=canonical(forged_result), rd=digest(forged_result))
    elif mode == 'source_digest':
        forge_allocation(db, 'UPDATE leave_carry_allocations SET source_digest=:d', d='0' * 64)
    elif mode == 'window_consistent':
        forged_result = {**result, 'available_through': '2026-12-31'}
        forged = {**request, 'plan': forged_result, 'reviewed_digest': digest(forged_result)}
        forge_allocation(db, 'UPDATE leave_carry_allocations SET request_json=:q, request_digest=:qd, '
            'result_json=:r, result_digest=:rd', q=canonical(forged), qd=digest(forged),
            r=canonical(forged_result), rd=digest(forged_result))
    else:
        forge_allocation(db, 'UPDATE leave_carry_allocations SET source_year=2024')
    with pytest.raises(ValueError):
        read(db, employee, kinds, identity)


@pytest.mark.parametrize('mode', ['delete', 'duplicate', 'actor', 'reason', 'new_value'])
def test_forged_audit_refuses(prior_year, mode):
    db, employee, kinds, identity = recorded(prior_year)
    where = "action='leave_carry_allocated'"
    db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
    if mode == 'delete':
        db.execute(text(f'DELETE FROM audit_logs WHERE {where}'))
    elif mode == 'duplicate':
        db.execute(text('INSERT INTO audit_logs(actor,action,entity_type,entity_id,old_value,new_value,reason,created_at) '
            f'SELECT actor,action,entity_type,entity_id,old_value,new_value,reason,created_at FROM audit_logs WHERE {where}'))
    elif mode == 'actor':
        db.execute(text(f"UPDATE audit_logs SET actor='someone-else' WHERE {where}"))
    elif mode == 'reason':
        db.execute(text(f"UPDATE audit_logs SET reason='Other' WHERE {where}"))
    else:
        db.execute(text(f"UPDATE audit_logs SET new_value=replace(new_value,'37/7','38/7') WHERE {where}"))
    db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError):
        read(db, employee, kinds, identity)


@pytest.mark.parametrize('change', ['inactive', 'nontracking'])
def test_type_made_inactive_or_nontracking_after_grant_refuses(prior_year, change):
    from app.models import LeaveType
    db, employee, kinds, identity = recorded(prior_year)
    kind = db.get(LeaveType, kinds['W'])
    if change == 'inactive':
        kind.active = False
    else:
        kind.tracks_balance = False
    db.flush()
    with pytest.raises(ValueError):
        read(db, employee, kinds, identity)


def _custom_allocation(prior_year, balance):
    from decimal import Decimal
    from app.leave_year_attribution_store import apply_year_attribution, review_year_attribution
    from app.models import LeaveType
    db, employee, kinds, args = prior_year
    kind = LeaveType(code='synthetic_custom_availability', name_ar='Synthetic', name_en='Synthetic',
        tracks_balance=True, active=True)
    db.add(kind); db.flush()
    if balance is not None:
        ledger = LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kind.id,
            transaction_type='opening', days_delta=Decimal(balance),
            reason='Synthetic separately reviewed opening', created_by='synthetic')
        db.add(ledger); db.flush()
        identity = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
        attribution = dict(ledger_id=ledger.id, allocations=((2025, Decimal(balance)),),
            reviewed_on=args['reviewed_on'], reason='Synthetic explicit source year', **identity)
        reviewed = review_year_attribution(db, **attribution)
        apply_year_attribution(db, operation_id='a' * 64, reviewed_digest=reviewed['reviewed_digest'],
            human_confirmed=True, **attribution)
    selection = call({**args, 'leave_type_id': kind.id})
    if balance is None or Decimal(balance) <= 0:
        selection.update(available_from=None, available_through=None)
    review = review_carry_allocation(db, **selection)
    record_carry_allocation(db, operation_id=OPERATION, reviewed_digest=review['reviewed_digest'],
        human_confirmed=True, **selection)
    identity = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    return db, employee, kind, identity


def test_custom_type_positive_by_stable_id(prior_year):
    db, employee, kind, identity = _custom_allocation(prior_year, '2.125')
    result = read_carry_availability(db, allocation_id=OPERATION, employee_id=employee.id,
        leave_type_id=kind.id, work_date=date(2026, 2, 1), **identity)
    assert result['leave_type_code'] == 'synthetic_custom_availability'
    assert result['source_amount'] == '17/8' and result['available_grant_ceiling'] == '17/8'
    assert result['eligible']


def test_debt_carry_is_not_positive_availability(prior_year):
    db, employee, kind, identity = _custom_allocation(prior_year, '-0.125')
    result = read_carry_availability(db, allocation_id=OPERATION, employee_id=employee.id,
        leave_type_id=kind.id, work_date=date(2026, 2, 1), **identity)
    assert result['kind'] == 'debt' and result['eligible'] is False
    assert result['available_grant_ceiling'] == '0/1' and result['debt_amount'] == '1/8'
    assert result['available_from'] is None and result['available_through'] is None


def test_zero_source_is_not_credit(prior_year):
    db, employee, kind, identity = _custom_allocation(prior_year, None)
    result = read_carry_availability(db, allocation_id=OPERATION, employee_id=employee.id,
        leave_type_id=kind.id, work_date=date(2026, 2, 1), **identity)
    assert result['kind'] == 'zero' and result['eligible'] is False
    assert result['source_amount'] == '0/1' and result['available_grant_ceiling'] == '0/1'


# --- Pure kernel: no database -------------------------------------------------

def test_pure_window_boundaries_and_signs():
    window = dict(available_from=date(2026, 1, 1), available_through=date(2026, 4, 30))
    assert evaluate_carry_window(carried_amount=Fraction(37, 7), work_date=date(2026, 1, 1), **window)['eligible']
    assert evaluate_carry_window(carried_amount=Fraction(37, 7), work_date=date(2026, 4, 30), **window)['eligible']
    outside = evaluate_carry_window(carried_amount=Fraction(37, 7), work_date=date(2026, 5, 1), **window)
    assert not outside['eligible'] and outside['preserved_unavailable'] == Fraction(37, 7)
    debt = evaluate_carry_window(carried_amount=Fraction(-1, 8), available_from=None,
        available_through=None, work_date=date(2026, 2, 1))
    assert debt['kind'] == 'debt' and debt['debt_amount'] == Fraction(1, 8) and not debt['eligible']
    zero = evaluate_carry_window(carried_amount=Fraction(), available_from=None,
        available_through=None, work_date=date(2026, 2, 1))
    assert zero['kind'] == 'zero' and zero['available_grant_ceiling'] == 0
    with pytest.raises(ValueError):
        evaluate_carry_window(carried_amount=Fraction(1), available_from=None,
            available_through=None, work_date=date(2026, 2, 1))
    with pytest.raises(ValueError):
        evaluate_carry_window(carried_amount=Fraction(-1), work_date=date(2026, 2, 1), **window)


def test_pure_evidence_binding_and_consistent_digest_attack():
    result = dict(employee_id=1, leave_type_id=2, source_year=2025, target_year=2026,
        source_digest='a' * 64, source={}, carried_amount='37/7', ledger_delta='0/1',
        cash_amount='0/1', held_annual='0/1', available_from='2026-01-01',
        available_through='2026-04-30', reason='Synthetic', operational_spending_allowed=False)
    request = dict(plan=result, reviewed_digest=digest(result), actor_id=1,
        actor_username='synthetic-admin', session_id='synthetic-session', session_version=0,
        restore_epoch=None)
    audits = (('synthetic-admin', 'a' * 64, canonical(result), 'Synthetic'),)
    base = dict(row_id='e' * 64, row_employee_id=1, row_leave_type_id=2, row_source_year=2025,
        row_request_digest=digest(request), row_source_digest='a' * 64,
        row_result_digest=digest(result), row_request_json=canonical(request),
        row_result_json=canonical(result), audits=audits, original_actor_username='synthetic-admin')
    assert verify_allocation_evidence(**base)[1] == result
    forged_result = {**result, 'carried_amount': '99/1'}
    forged_request = {**request, 'plan': forged_result, 'reviewed_digest': digest(forged_result)}
    with pytest.raises(ValueError):
        verify_allocation_evidence(**{**base, 'row_request_json': canonical(forged_request),
            'row_request_digest': digest(forged_request), 'row_result_json': canonical(forged_result),
            'row_result_digest': digest(forged_result)})
    with pytest.raises(ValueError):
        verify_allocation_evidence(**{**base, 'audits': ()})
    with pytest.raises(ValueError):
        verify_allocation_evidence(**{**base, 'row_result_json': canonical({**result, 'extra': 1}),
            'row_result_digest': digest({**result, 'extra': 1})})
