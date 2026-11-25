from datetime import date, datetime
from fractions import Fraction
import pytest
from app.leave_cash_out import review_cash_out, CAIRO_TZ
from tests.test_leave_carry_consumption import ready, FULL  # noqa
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_cash_out import payroll


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_backdated_evaluation_date_cannot_bypass_expired_carry_refusal(prior_year):
    assert datetime.now(CAIRO_TZ).date() > date(2026, 5, 1)    # today is after the carry window closed
    db, call = ready(prior_year, category='A')
    _r, item = payroll(db, prior_year[1], start=date(2026, 1, 1), end=date(2026, 1, 31), status='draft')
    auth = {k: call[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    kw = dict(employee_id=call['employee_id'], leave_type_id=call['leave_type_id'], days=Fraction(1, 4),
              payroll_item_id=item.id, **auth)
    with pytest.raises(ValueError, match='Expired carry remains unresolved'):
        review_cash_out(db, evaluated_on=date(2026, 5, 1), **kw)
    with pytest.raises(ValueError, match='Expired carry remains unresolved'):
        review_cash_out(db, evaluated_on=date(2026, 4, 30), **kw)   # backdated to inside the window
