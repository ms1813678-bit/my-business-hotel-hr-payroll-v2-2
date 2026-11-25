from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError

from app.models import LeaveBalanceTransaction, LeaveType, LeaveYearAttribution, AuditLog
from app.leave_year_attribution_store import review_year_attribution, apply_year_attribution
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa


@pytest.fixture
def source(history_context):
    db, actor, auth, fact = history_context
    kind = db.scalar(select(LeaveType).where(LeaveType.code == 'annual'))
    row = LeaveBalanceTransaction(employee_id=fact.employee_id, leave_type_id=kind.id,
        transaction_type='opening', days_delta=Decimal(10), reason='Synthetic existing opening', created_by=actor.username)
    db.add(row); db.flush()
    args = dict(ledger_id=row.id, allocations=((2025, Decimal(3)), (2026, Decimal(7))),
        reviewed_on=date(2026, 11, 25), reason='Synthetic explicit source year attribution', **auth)
    return db, row, args


def apply(db, args, operation='a'*64):
    review = review_year_attribution(db, **args)
    call = dict(operation_id=operation, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **args)
    return apply_year_attribution(db, **call), call


def test_exact_attribution_receipt_no_balance_delta_replay_no_duplicate(source):
    db, row, args = source
    before = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    result, call = apply(db, args)
    assert result['ledger_delta'] == '0/1' and result['proves_year_completeness'] is False
    assert result['allocations'] == [[2025, '3/1'], [2026, '7/1']]
    assert apply_year_attribution(db, **call) == result
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before
    db.refresh(row)
    assert row.days_delta == 10 and row.earning_year is None
    assert db.scalar(select(func.count()).select_from(LeaveYearAttribution)) == 1
    with pytest.raises(ValueError, match='already attributed'): apply(db, args, 'b'*64)


@pytest.mark.parametrize('change', ['source', 'reason', 'version', 'confirmation'])
def test_stale_authority_or_review_refuses_without_receipt(source, change):
    db, row, args = source
    review = review_year_attribution(db, **args)
    call = dict(operation_id='a'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **args)
    if change == 'source': row.reason = 'Synthetic source edited'; db.flush()
    if change == 'reason': call['reason'] = 'Synthetic different reason'
    if change == 'version': call['session_version'] += 1
    if change == 'confirmation': call['human_confirmed'] = False
    with pytest.raises(ValueError): apply_year_attribution(db, **call)
    assert db.scalar(select(func.count()).select_from(LeaveYearAttribution)) == 0


@pytest.mark.parametrize('verb', ['UPDATE', 'DELETE', 'TRUNCATE'])
def test_receipt_is_immutable_in_real_pg(source, verb):
    db, row, args = source
    apply(db, args)
    with pytest.raises(DBAPIError):
        with db.begin_nested():
            sql = {'UPDATE': "UPDATE leave_year_attributions SET result_digest='x'", 'DELETE': 'DELETE FROM leave_year_attributions', 'TRUNCATE': 'TRUNCATE leave_year_attributions'}[verb]
            db.execute(text(sql))
    assert db.scalar(select(func.count()).select_from(LeaveYearAttribution)) == 1


def test_audit_failure_rolls_back_receipt_without_changing_source(source):
    from sqlalchemy import event
    db, row, args = source
    review = review_year_attribution(db, **args)
    def fail(session, ctx, objects):
        if any(isinstance(o, AuditLog) and o.action == 'leave_year_attributed' for o in session.new):
            raise RuntimeError('Synthetic audit failure')
    event.listen(db, 'before_flush', fail)
    try:
        with pytest.raises(RuntimeError):
            apply_year_attribution(db, operation_id='a'*64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **args)
    finally: event.remove(db, 'before_flush', fail)
    assert db.scalar(select(func.count()).select_from(LeaveYearAttribution)) == 0
    assert db.get(LeaveBalanceTransaction, row.id).days_delta == 10


def test_reviewed_attribution_enables_source_year_preview_but_not_year_close(source):
    from app.leave_carry_source import review_carry_source
    db, row, args = source
    apply(db, args)
    identity = {k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    result = review_carry_source(db, employee_id=row.employee_id, leave_type_id=row.leave_type_id,
        source_year=2026, reviewed_on=date(2026, 11, 25), **identity)
    assert result['amount'] == 7 and result['source']['year_attributions']
    assert not result['source_verified'] and not result['posting_allowed']
