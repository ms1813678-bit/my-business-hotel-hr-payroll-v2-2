from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from app.leave_zero_decision import review_zero_decision, record_zero_decision
from app.leave_carry_allocation_store import review_carry_allocation, record_carry_allocation
from app.leave_cash_out import verified_released_balance
from app.leave_carry_source import review_carry_source
from app.models import LeaveZeroDecision, LeaveBalanceTransaction, AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa

IDENT = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


def zero(db, args, op='a', reason='Synthetic HR zero choice'):
    sel = dict(source_selection=args, reason=reason)
    review = review_zero_decision(db, **sel)
    confirm = dict(operation_id=op * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **sel)
    return review, confirm


def test_zero_review_record_replay_writes_exact_exclusion_and_balance_is_zero(prior_year):
    db, employee, kinds, args = prior_year
    review, confirm = zero(db, args)
    assert review['plan']['zeroed_amount'] == '37/7' and review['plan']['ledger_delta'] == '-37/7'
    assert review['plan']['cash_amount'] == '0/1' and not review['posting_allowed']
    before = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    first = record_zero_decision(db, **confirm)
    assert first == record_zero_decision(db, **confirm)          # exact replay, no second debit
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before + 1
    assert db.scalar(select(func.count()).select_from(LeaveZeroDecision)) == 1
    ident = {k: args[k] for k in IDENT}
    balance = verified_released_balance(db, employee_id=employee.id, leave_type_id=args['leave_type_id'], identity=ident)
    assert balance['balance'] == 0                                # W leftover really zeroed, other types untouched


def test_zero_then_source_review_proves_debit_and_carry_is_exclusive(prior_year):
    db, employee, kinds, args = prior_year
    _r, confirm = zero(db, args)
    record_zero_decision(db, **confirm)
    ident = {k: args[k] for k in IDENT}
    src = review_carry_source(db, employee_id=employee.id, leave_type_id=args['leave_type_id'], source_year=2025,
        reviewed_on=args['reviewed_on'], **ident)
    assert src['amount'] == 0
    with pytest.raises(ValueError, match='exclusive'):
        review_carry_allocation(db, source_selection=args, reason='Synthetic')
    with pytest.raises(ValueError, match='No positive leftover|already zeroed'):
        record_zero_decision(db, **{**confirm, 'operation_id': 'b' * 64})


def test_carry_first_blocks_zero(prior_year):
    db, employee, kinds, args = prior_year
    sel = dict(source_selection=args, available_from=date(2026, 1, 1), available_through=date(2026, 4, 30), reason='Synthetic')
    rv = review_carry_allocation(db, **sel)
    record_carry_allocation(db, operation_id='c' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, **sel)
    with pytest.raises(ValueError, match='already carried'):
        review_zero_decision(db, source_selection=args, reason='Synthetic')


def test_annual_negative_and_zero_leftover_are_refused(prior_year):
    db, employee, kinds, args = prior_year
    with pytest.raises(ValueError, match='weekly rest and official holiday'):
        review_zero_decision(db, source_selection={**args, 'leave_type_id': kinds['A']}, reason='Synthetic')


@pytest.mark.parametrize('mutate', ['unconfirmed', 'bad_digest', 'bad_op', 'empty_reason', 'stale_register'])
def test_unconfirmed_stale_or_malformed_refuses_without_writes(prior_year, mutate):
    db, employee, kinds, args = prior_year
    review, confirm = zero(db, args)
    if mutate == 'unconfirmed': confirm['human_confirmed'] = False
    if mutate == 'bad_digest': confirm['reviewed_digest'] = '0' * 64
    if mutate == 'bad_op': confirm['operation_id'] = 'xyz'
    if mutate == 'empty_reason': confirm['reason'] = '  '
    if mutate == 'stale_register':
        from app.models import LeaveRegisterEntry
        row = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == employee.id,
            LeaveRegisterEntry.work_date == date(2025, 12, 31)))
        row.code = 'A' if row.code != 'A' else ''
        db.flush()
    with pytest.raises(ValueError):
        record_zero_decision(db, **confirm)
    assert db.scalar(select(func.count()).select_from(LeaveZeroDecision)) == 0


def test_later_year_debit_blocks_zero_without_guessing(prior_year):
    db, employee, kinds, args = prior_year
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=args['leave_type_id'], transaction_type='adjustment',
        days_delta=-1, reason='Synthetic later usage', created_by='synthetic', source_operation_key='month_earning:x:y',
        source_digest='1' * 64, earning_year=2026, released_on=date(2026, 1, 31)))
    db.flush()
    with pytest.raises(ValueError):
        review_zero_decision(db, source_selection=args, reason='Synthetic')


def test_receipt_immutable_and_forgery_modes_detected(prior_year):
    db, employee, kinds, args = prior_year
    _r, confirm = zero(db, args)
    record_zero_decision(db, **confirm)
    for sql in ['UPDATE leave_zero_decisions SET source_year=source_year', 'DELETE FROM leave_zero_decisions',
                'TRUNCATE leave_zero_decisions']:
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.execute(text(sql))
    ident = {k: args[k] for k in IDENT}
    for table, stmt in [('leave_balance_transactions', "UPDATE leave_balance_transactions SET reason='forged' WHERE source_operation_key LIKE 'leave_zero:%'"),
                        ('audit_logs', "UPDATE audit_logs SET reason='forged' WHERE action='leave_zero_decided'")]:
        db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
        db.execute(text(stmt))
        db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
        db.expire_all()
        with pytest.raises(ValueError):
            verified_released_balance(db, employee_id=employee.id, leave_type_id=args['leave_type_id'], identity=ident)
        db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
        db.execute(text(stmt.replace("'forged'", "'Synthetic HR zero choice'")))
        db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
        db.expire_all()


@pytest.mark.parametrize('bad', ['non_str_reason', 'missing_key', 'other_reviewed_on_replay'])
def test_malformed_input_is_value_error_and_replay_binds_review_date(prior_year, bad):
    db, employee, kinds, args = prior_year
    review, confirm = zero(db, args)
    if bad == 'non_str_reason':
        with pytest.raises(ValueError):
            review_zero_decision(db, source_selection=args, reason=5)
        with pytest.raises(ValueError):
            record_zero_decision(db, **{**confirm, 'reason': None})
    if bad == 'missing_key':
        sel = {k: v for k, v in args.items() if k != 'reviewed_on'}
        with pytest.raises(ValueError):
            review_zero_decision(db, source_selection=sel, reason='Synthetic')
        with pytest.raises(ValueError):
            record_zero_decision(db, **{**confirm, 'source_selection': sel})
    if bad == 'other_reviewed_on_replay':
        record_zero_decision(db, **confirm)
        with pytest.raises(ValueError):
            record_zero_decision(db, **{**confirm, 'source_selection': {**args, 'reviewed_on': date(2026, 1, 2)}})
    assert db.scalar(select(func.count()).select_from(LeaveZeroDecision)) == (1 if bad == 'other_reviewed_on_replay' else 0)
