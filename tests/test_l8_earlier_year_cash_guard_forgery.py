"""Earlier-year annual cash is ignored ONLY after canonical verification. Forged earlier evidence must fail closed.
Year-2 end to end is not buildable (held transition); the guard is driven directly on a real 2025 settlement."""
import pytest
from sqlalchemy import text
from app.leave_annual_settlement_store import _blocking_prior_cash
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_l8_source_year_cash_binding import settle, FULL


def sql(db, table, stmt, **p):
    db.execute(text(f'ALTER TABLE {table} DISABLE TRIGGER USER'))
    db.execute(text(stmt), p)
    db.execute(text(f'ALTER TABLE {table} ENABLE TRIGGER USER'))
    db.expire_all()


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_clean_earlier_year_cash_ignored_only_for_a_later_year(prior_year):
    db, employee, kinds, item, auth = settle(prior_year)
    assert _blocking_prior_cash(db, employee.id, kinds['A'], 2026) is None
    assert _blocking_prior_cash(db, employee.id, kinds['A'], 2025)      # same year blocks
    assert _blocking_prior_cash(db, employee.id, kinds['A'], 2024)      # earlier-than-cash year blocks


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
@pytest.mark.parametrize('mode', ['audit_actor', 'audit_reason', 'audit_deleted', 'audit_new_value',
                                  'annual_year_to_2024', 'annual_year_to_2027', 'cash_days', 'cash_source_year',
                                  'annual_result_json', 'annual_request_json', 'ledger_actor'])
def test_forged_earlier_year_evidence_never_lets_later_year_pass(prior_year, mode):
    db, employee, kinds, item, auth = settle(prior_year)
    aud = "action='leave_annual_settled'"
    stmts = {
        'audit_actor': ('audit_logs', f"UPDATE audit_logs SET actor='x' WHERE {aud}"),
        'audit_reason': ('audit_logs', f"UPDATE audit_logs SET reason='x' WHERE {aud}"),
        'audit_deleted': ('audit_logs', f"DELETE FROM audit_logs WHERE {aud}"),
        'audit_new_value': ('audit_logs', f"UPDATE audit_logs SET new_value=new_value||' ' WHERE {aud}"),
        'annual_year_to_2024': ('leave_annual_settlements', "UPDATE leave_annual_settlements SET source_year=2024"),
        'annual_year_to_2027': ('leave_annual_settlements', "UPDATE leave_annual_settlements SET source_year=2027"),
        'cash_days': ('leave_cash_outs', "UPDATE leave_cash_outs SET days_canonical='1/1'"),
        'cash_source_year': ('leave_cash_outs', "UPDATE leave_cash_outs SET source_json=replace(source_json, chr(34)||'source_year'||chr(34)||chr(58)||'2025', chr(34)||'source_year'||chr(34)||chr(58)||'2024')"),
        'annual_result_json': ('leave_annual_settlements', "UPDATE leave_annual_settlements SET result_json=replace(result_json,'15/2','8/1')"),
        'annual_request_json': ('leave_annual_settlements', "UPDATE leave_annual_settlements SET request_json=replace(request_json,'2025','2024')"),
        'ledger_actor': ('leave_balance_transactions', "UPDATE leave_balance_transactions SET created_by='x' WHERE transaction_type='leave_cash_out' OR source_operation_key IS NOT NULL AND id=(SELECT ledger_id FROM leave_cash_outs LIMIT 1)"),
    }
    table, stmt = stmts[mode]
    sql(db, table, stmt)
    try:
        blocked = _blocking_prior_cash(db, employee.id, kinds['A'], 2026)
    except Exception:
        return
    assert blocked, mode
