from datetime import date
from decimal import Decimal
from fractions import Fraction
import pytest
from sqlalchemy import select
from app.leave_completed_year_source import review_completed_year_source
from app.models import AdminAccount, LeaveBalanceTransaction
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_month_earning import monthly  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa


def test_digest_stable_and_read_only_and_annual_stays_held(prior_year):
    db, employee, kinds, args = prior_year
    first = review_completed_year_source(db, **args)
    again = review_completed_year_source(db, **args)
    assert first['source_digest'] == again['source_digest']
    assert not (db.new or db.dirty or db.deleted)
    annual = review_completed_year_source(db, **{**args, 'leave_type_id': kinds['A']})
    assert annual['posting_allowed'] is False and annual['source_verified'] is True
    assert annual['amount'] > 0 and annual['held_annual'] == 0  # earned annual already in ledger, not double counted


@pytest.mark.parametrize('tag', [2025, None])
def test_extra_unproven_ledger_row_refuses_completion(prior_year, tag):
    db, employee, kinds, args = prior_year
    db.add(LeaveBalanceTransaction(employee_id=employee.id, leave_type_id=kinds['W'], transaction_type='adjustment',
        days_delta=Decimal(1), reason='Synthetic unproven', created_by='synthetic', earning_year=tag,
        source_operation_key='synthetic:forged' if tag else None, source_digest='f' * 64 if tag else None,
        released_on=date(2026, 1, 1) if tag else None))
    db.flush()
    with pytest.raises(ValueError):
        review_completed_year_source(db, **args)


@pytest.mark.parametrize('field,value', [('reviewed_on', date(2025, 12, 31)), ('reviewed_on', date(2999, 1, 1)),
                                         ('source_year', 2024), ('source_year', True), ('source_year', 2026)])
def test_bad_year_or_review_date_refuses(prior_year, field, value):
    db, employee, kinds, args = prior_year
    with pytest.raises(ValueError):
        review_completed_year_source(db, **{**args, field: value})


def test_finance_and_assistant_roles_refuse_completed_source(prior_year):
    db, employee, kinds, args = prior_year
    for role in ('finance', 'assistant_manager'):
        db.get(AdminAccount, args['actor_id']).role = role; db.flush()
        with pytest.raises(ValueError):
            review_completed_year_source(db, **args)
