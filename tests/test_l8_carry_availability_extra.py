import pytest
from sqlalchemy import text
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_carry_availability import recorded, read


def tamper(db, sql):
    for t in ('leave_balance_transactions', 'leave_month_earnings'):
        db.execute(text(f'ALTER TABLE {t} DISABLE TRIGGER USER'))
    db.execute(text(sql))
    for t in ('leave_balance_transactions', 'leave_month_earnings'):
        db.execute(text(f'ALTER TABLE {t} ENABLE TRIGGER USER'))
    db.expire_all()


@pytest.mark.parametrize('mode', ['ledger_amount', 'ledger_year', 'ledger_digest', 'ledger_deleted_type',
                                  'monthly_digest'])
def test_live_ledger_or_monthly_receipt_tamper_after_grant_refuses(prior_year, mode):
    db, employee, kinds, identity = recorded(prior_year)
    sel = "SELECT id FROM leave_balance_transactions WHERE leave_type_id=%d AND earning_year=2025 ORDER BY id LIMIT 1" % kinds['W']
    ledger = "id=(%s)" % sel
    sql = {
        'ledger_amount': f"UPDATE leave_balance_transactions SET exact_numerator=exact_numerator+2 WHERE {ledger}",
        'ledger_year': f"UPDATE leave_balance_transactions SET earning_year=2026 WHERE {ledger}",
        'ledger_digest': f"UPDATE leave_balance_transactions SET source_digest=repeat('0',64) WHERE {ledger}",
        'ledger_deleted_type': f"UPDATE leave_balance_transactions SET leave_type_id={kinds['H']} WHERE {ledger}",
        'monthly_digest': "UPDATE leave_month_earnings SET result_digest=repeat('0',64) WHERE id=(SELECT id FROM leave_month_earnings ORDER BY month_start LIMIT 1)",
    }[mode]
    tamper(db, sql)
    with pytest.raises(Exception):
        read(db, employee, kinds, identity)
