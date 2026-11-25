from datetime import date
import pytest
from sqlalchemy import select, text
from app.models import AdminAccount, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveMonthEarning, PayrollRun, LeaveYearTransition
from app.leave_month_earning import review_month_earning
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_next_year_earning import next_month
from tests.test_leave_year_transition import FULL

P = [{'hire_date': FULL, 'used_annual': 6}]


def off(db, table, stmt, **p):
    db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
    db.execute(text(stmt), p)
    db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
    db.expire_all()


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_clean_review_passes(prior_year):
    db, call = next_month(prior_year)
    assert review_month_earning(db, **call)['plan']['year_transitions']


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_missing_checkpoint_refuses(prior_year):
    db, call = next_month(prior_year)
    off(db, 'leave_year_transitions', 'DELETE FROM leave_year_transitions')
    with pytest.raises(ValueError, match='checkpoint required'):
        review_month_earning(db, **call)


@pytest.mark.parametrize('prior_year', P, indirect=True)
def test_two_year_gap_needs_the_intervening_checkpoint(prior_year):
    """A 2027 month on a 2025 baseline with only the 2025 checkpoint must refuse (needs 2026 checkpoint)."""
    db, call = next_month(prior_year)
    call = {**call, 'month_start': date(2027, 1, 1), 'evaluated_on': date(2027, 2, 1),
            'current_insurance_years': {(k[0], date(2027, 1, 1)): v for k, v in call['current_insurance_years'].items()},
            'age_evaluation_dates': {date(2027, 1, 1): date(2027, 1, 1)}}
    with pytest.raises(ValueError):
        review_month_earning(db, **call)


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('mode', ['audit_deleted', 'audit_actor', 'audit_reason', 'audit_value', 'result_digest',
                                  'target_year', 'request_epoch', 'selection', 'opening_credit_flag'])
def test_tampered_checkpoint_or_audit_refuses(prior_year, mode):
    db, call = next_month(prior_year)
    a = "action='leave_year_transition_recorded'"
    stmts = {
        'audit_deleted': ('audit_logs', f'DELETE FROM audit_logs WHERE {a}'),
        'audit_actor': ('audit_logs', f"UPDATE audit_logs SET actor='x' WHERE {a}"),
        'audit_reason': ('audit_logs', f"UPDATE audit_logs SET reason='x' WHERE {a}"),
        'audit_value': ('audit_logs', f"UPDATE audit_logs SET new_value=new_value||' ' WHERE {a}"),
        'result_digest': ('leave_year_transitions', "UPDATE leave_year_transitions SET result_digest=repeat('0',64)"),
        'target_year': ('leave_year_transitions', "UPDATE leave_year_transitions SET source_year=2024, target_year=2025"),
        'request_epoch': ('leave_year_transitions', "UPDATE leave_year_transitions SET request_json=replace(request_json,'synthetic','changed')"),
        'selection': ('leave_year_transitions', "UPDATE leave_year_transitions SET result_json=replace(result_json,'selection_id','selection_idx')"),
        'opening_credit_flag': ('leave_year_transitions', "UPDATE leave_year_transitions SET result_json=replace(result_json,'new_opening_credit','new_opening_creditx')"),
    }
    off(db, *stmts[mode])
    with pytest.raises(Exception):
        review_month_earning(db, **call)


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('mode', ['named_ledger_amount', 'named_ledger_deleted_row_type', 'closing_receipt_deleted',
                                  'monthly_receipt_deleted', 'extra_monthly_receipt', 'register_changed'])
def test_old_evidence_changes_refuse(prior_year, mode):
    db, call = next_month(prior_year)
    if mode == 'named_ledger_amount':
        off(db, 'leave_balance_transactions', "UPDATE leave_balance_transactions SET exact_numerator=exact_numerator+2 WHERE id=(SELECT id FROM leave_balance_transactions WHERE exact_numerator IS NOT NULL AND earning_year=2025 ORDER BY id LIMIT 1)")
    elif mode == 'named_ledger_deleted_row_type':
        off(db, 'leave_balance_transactions', "UPDATE leave_balance_transactions SET leave_type_id=(SELECT id FROM leave_types WHERE code='official_holiday') WHERE id=(SELECT id FROM leave_balance_transactions WHERE exact_numerator IS NOT NULL AND earning_year=2025 ORDER BY id LIMIT 1)")
    elif mode == 'closing_receipt_deleted':
        off(db, 'leave_carry_allocations', "DELETE FROM leave_carry_allocations WHERE id=repeat('1',64)")
    elif mode == 'monthly_receipt_deleted':
        db.execute(text('ALTER TABLE leave_month_earnings DISABLE TRIGGER USER'))
        db.execute(text("UPDATE leave_month_earnings SET result_digest=repeat('0',64) WHERE id=(SELECT id FROM leave_month_earnings ORDER BY month_start LIMIT 1)"))
        db.execute(text('ALTER TABLE leave_month_earnings ENABLE TRIGGER USER')); db.expire_all()
    elif mode == 'extra_monthly_receipt':
        from app.models import LeaveMonthEarning
        original=db.scalar(select(LeaveMonthEarning).order_by(LeaveMonthEarning.month_start))
        values={column.name:getattr(original,column.name) for column in LeaveMonthEarning.__table__.columns}
        values['id']='9'*64
        # A source-month uniqueness constraint itself correctly refuses an extra
        # duplicate before the coverage verifier can see it.
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            with db.begin_nested():db.execute(LeaveMonthEarning.__table__.insert().values(**values))
        return
    else:
        from app.models import LeaveRegisterEntry
        row = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == prior_year[1].id,
            LeaveRegisterEntry.work_date == date(2025, 12, 31)))
        row.code = 'U'; db.flush()
    with pytest.raises(Exception):
        review_month_earning(db, **call)


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('mode', ['role', 'epoch', 'version', 'closed_payroll'])
def test_current_authority_and_closed_payroll_refuse(prior_year, mode):
    from datetime import date as d
    db, call = next_month(prior_year)
    if mode == 'role':
        db.get(AdminAccount, call['actor_id']).role = 'assistant_manager'; db.flush()
    elif mode == 'epoch':
        call = {**call, 'expected_epoch': 'changed-epoch'}
    elif mode == 'version':
        call = {**call, 'session_version': call['session_version'] + 1}
    else:
        run = db.scalar(select(PayrollRun).limit(1))
        assert run is not None
        db.execute(text("UPDATE payroll_runs SET period_start='2026-01-01', period_end='2026-01-31', status='approved' WHERE id=:i"), {'i': run.id})
        db.expire_all()
    with pytest.raises(Exception):
        review_month_earning(db, **call)
