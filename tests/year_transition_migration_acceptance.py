from datetime import date
import pytest
from app.leave_year_transition import review_year_transition,record_year_transition
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_year_transition import prepared,FULL

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_populate_real_checkpoint(prior_year):
    db,sources=prepared(prior_year)
    call=dict(source_selections=sources,reason='Synthetic persisted checkpoint')
    review=review_year_transition(db,**call)
    record_year_transition(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    db.commit()
