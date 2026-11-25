import pytest
from app.leave_carry_consumption import review_carry_consumption,record_carry_consumption
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_carry_consumption import ready,FULL

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_populate_real_consumption(prior_year):
    db,call=ready(prior_year)
    review=review_carry_consumption(db,**call)
    record_carry_consumption(db,operation_id='a'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    db.commit()
