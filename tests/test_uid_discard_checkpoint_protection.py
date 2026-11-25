from datetime import date
import pytest
from sqlalchemy import text
from app.leave_year_transition import review_year_transition,record_year_transition
from tests.sync_uid_test_support import discard_sync_uid_for_older_downgrade
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_year_transition import prepared,FULL

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_test_only_uid_helper_preserves_populated_checkpoint_guard(prior_year):
    db,sources=prepared(prior_year);call=dict(source_selections=sources,reason='Synthetic checkpoint')
    review=review_year_transition(db,**call)
    record_year_transition(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    with pytest.raises(RuntimeError,match='year transition evidence exists'):
        with db.begin_nested():discard_sync_uid_for_older_downgrade(db.connection())
    assert db.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name='employees' AND column_name='sync_uid'"))==1
    assert db.scalar(text('SELECT count(*) FROM leave_year_transitions'))==1
