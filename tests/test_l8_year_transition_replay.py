from decimal import Decimal
import pytest
from sqlalchemy import select, func, text
from app.models import LeaveBalanceTransaction, LeaveYearTransition
from app.leave_year_transition import review_year_transition, record_year_transition
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_year_transition import prepared
from tests.test_l8_source_year_cash_binding import FULL


def later_debit(db, sources):
    db.add(LeaveBalanceTransaction(employee_id=sources['A']['employee_id'], leave_type_id=sources['A']['leave_type_id'],
        transaction_type='leave_use', days_delta=Decimal(-1), reason='Synthetic 2026 use', created_by='synthetic'))
    db.flush()


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_new_checkpoint_refuses_once_unrelated_later_ledger_exists(prior_year):
    """Fail-closed: the 'closing' balance can never silently include post-year rows."""
    db, sources = prepared(prior_year)
    later_debit(db, sources)
    with pytest.raises(ValueError, match='unrelated ledger'):
        review_year_transition(db, source_selections=sources, reason='Synthetic')


def confirm_args(db, sources):
    call = dict(source_selections=sources, reason='Synthetic')
    review = review_year_transition(db, **call)
    return dict(operation_id='d' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_lost_response_replay_is_idempotent_after_later_ledger_growth(prior_year):
    db, sources = prepared(prior_year)
    confirm = confirm_args(db, sources)
    first = record_year_transition(db, **confirm)
    later_debit(db, sources)
    assert record_year_transition(db, **confirm) == first


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
@pytest.mark.parametrize('mode', ['digest', 'reason', 'other_operation_same_year', 'audit_deleted', 'row_forged'])
def test_replay_still_refuses_wrong_or_forged_stored_evidence(prior_year, mode):
    db, sources = prepared(prior_year)
    confirm = confirm_args(db, sources)
    record_year_transition(db, **confirm)
    later_debit(db, sources)
    if mode == 'digest':
        confirm = {**confirm, 'reviewed_digest': '0' * 64}
    elif mode == 'reason':
        confirm = {**confirm, 'reason': 'Another reason'}
    elif mode == 'other_operation_same_year':
        confirm = {**confirm, 'operation_id': 'e' * 64}
    elif mode == 'audit_deleted':
        db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
        db.execute(text("DELETE FROM audit_logs WHERE action='leave_year_transition_recorded'"))
        db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER')); db.expire_all()
    else:
        db.execute(text('ALTER TABLE leave_year_transitions DISABLE TRIGGER USER'))
        db.execute(text("UPDATE leave_year_transitions SET result_json=replace(result_json,'3/2','5/2')"))
        db.execute(text('ALTER TABLE leave_year_transitions ENABLE TRIGGER USER')); db.expire_all()
    with pytest.raises(ValueError):
        record_year_transition(db, **confirm)
