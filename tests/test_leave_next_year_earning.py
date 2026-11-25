from datetime import date,timedelta
import pytest
from sqlalchemy import select,func
from app.models import LeaveRegisterEntry,LeaveBalanceTransaction
from app.leave_month_earning import review_month_earning,apply_month_earning
from app.leave_year_transition import review_year_transition,record_year_transition
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_year_transition import prepared,FULL


def next_month(prior_year):
    db,sources=prepared(prior_year);call=dict(source_selections=sources,reason='Synthetic')
    review=review_year_transition(db,**call)
    record_year_transition(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    selection=sources['A'];employee_id=selection['employee_id']
    day=date(2026,1,1)
    while day<=date(2026,1,31):
        db.add(LeaveRegisterEntry(employee_id=employee_id,work_date=day,code='W' if day.day in (3,10,17) else '',source='synthetic',recorded_by='synthetic'))
        day+=timedelta(days=1)
    db.flush()
    auth={k:selection[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    month=date(2026,1,1)
    return db,dict(selection_id=selection['selection_id'],month_start=month,evaluated_on=date(2026,2,1),
        current_insurance_years={(employee_id,month):None},age_evaluation_dates={month:month},**auth)


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_real_second_year_month_posts_usage_without_new_opening_credit(prior_year):
    db,call=next_month(prior_year)
    openings=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.transaction_type=='opening'))
    review=review_month_earning(db,**call)
    assert review['plan']['year_transitions'] and review['plan']['used']['W']==3
    assert review['plan']['tier']==21
    result=apply_month_earning(db,operation_id='b'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic second year',**call)
    assert any(item['category']=='W' and item['kind']=='usage' and item['amount']=='-3/1' for item in result['ledger'])
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.transaction_type=='opening'))==openings
    assert apply_month_earning(db,operation_id='b'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic second year',**call)==result


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_next_year_rechecks_prior_register_against_own_year(prior_year):
    db,call=next_month(prior_year)
    row=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==prior_year[1].id,LeaveRegisterEntry.work_date==date(2025,12,31)))
    row.code='U';db.flush()
    with pytest.raises(ValueError,match='prior earned source changed'):review_month_earning(db,**call)
