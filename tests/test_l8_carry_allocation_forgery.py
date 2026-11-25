import json
from datetime import date
import pytest
from sqlalchemy import select, text
from app.leave_carry_allocation_store import review_carry_allocation, record_carry_allocation
from app.leave_month_earning import canonical, digest
from app.models import AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_carry_allocation_store import call


def recorded(prior_year):
    db, employee, kinds, args = prior_year
    selection = call(args); review = review_carry_allocation(db, **selection)
    confirm = dict(operation_id='e' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    record_carry_allocation(db, **confirm)
    return db, confirm


def forge(db, sql, **params):
    db.execute(text('ALTER TABLE leave_carry_allocations DISABLE TRIGGER USER'))
    db.execute(text(sql), params)
    db.execute(text('ALTER TABLE leave_carry_allocations ENABLE TRIGGER USER'))
    db.expire_all()


@pytest.mark.parametrize('mode', ['result_only', 'result_digest_consistent', 'request_only', 'request_digest_consistent',
                                  'source_digest', 'both_consistent_other_amount', 'columns'])
def test_forged_carry_receipt_does_not_replay(prior_year, mode):
    db, confirm = recorded(prior_year)
    row = db.execute(text("SELECT request_json,result_json FROM leave_carry_allocations")).one()
    request, result = json.loads(row.request_json), json.loads(row.result_json)
    result2 = {**result, 'carried_amount': '99/1'}
    if mode == 'result_only':
        forge(db, 'UPDATE leave_carry_allocations SET result_json=:r', r=canonical(result2))
    elif mode == 'result_digest_consistent':
        forge(db, 'UPDATE leave_carry_allocations SET result_json=:r, result_digest=:d', r=canonical(result2), d=digest(result2))
    elif mode == 'request_only':
        forge(db, 'UPDATE leave_carry_allocations SET request_json=:r', r=canonical({**request, 'actor_id': 999}))
    elif mode == 'request_digest_consistent':
        forged = {**request, 'actor_id': 999}
        forge(db, 'UPDATE leave_carry_allocations SET request_json=:r, request_digest=:d', r=canonical(forged), d=digest(forged))
    elif mode == 'source_digest':
        forge(db, "UPDATE leave_carry_allocations SET source_digest=:d", d='0' * 64)
    elif mode == 'both_consistent_other_amount':
        forged = {**request, 'plan': result2}
        forge(db, 'UPDATE leave_carry_allocations SET request_json=:q, request_digest=:qd, result_json=:r, result_digest=:rd',
              q=canonical(forged), qd=digest(forged), r=canonical(result2), rd=digest(result2))
    else:
        forge(db, 'UPDATE leave_carry_allocations SET source_year=2024')
    with pytest.raises(ValueError):
        record_carry_allocation(db, **confirm)


@pytest.mark.parametrize('mode', ['delete', 'duplicate', 'new_value', 'actor', 'reason'])
def test_forged_carry_audit_does_not_replay(prior_year, mode):
    db, confirm = recorded(prior_year)
    where = "action='leave_carry_allocated'"
    db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))  # as a DBA or restore could
    if mode == 'delete':
        db.execute(text(f'DELETE FROM audit_logs WHERE {where}'))
    elif mode == 'duplicate':
        db.execute(text('INSERT INTO audit_logs(actor,action,entity_type,entity_id,old_value,new_value,reason,created_at) '
                        f'SELECT actor,action,entity_type,entity_id,old_value,new_value,reason,created_at FROM audit_logs WHERE {where}'))
    elif mode == 'new_value':
        db.execute(text(f"UPDATE audit_logs SET new_value=replace(new_value,'37/7','38/7') WHERE {where}"))
    elif mode == 'actor':
        db.execute(text(f"UPDATE audit_logs SET actor='someone-else' WHERE {where}"))
    else:
        db.execute(text(f"UPDATE audit_logs SET reason='Other' WHERE {where}"))
    db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError):
        record_carry_allocation(db, **confirm)


def test_replay_from_a_new_session_is_refused_not_silently_accepted(prior_year):
    from app.models import AdminSession
    db, confirm = recorded(prior_year)
    selection = confirm['source_selection']
    db.add(AdminSession(id='synthetic-second-session', admin_account_id=selection['actor_id'])); db.flush()
    other = {**confirm, 'source_selection': {**selection, 'session_id': 'synthetic-second-session'}}
    with pytest.raises(ValueError):
        record_carry_allocation(db, **other)
