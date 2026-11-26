# ruff: noqa: F811
"""Real later-year closing choices, no transition admission scaffold."""
from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select,func
from app.models import LeaveBalanceTransaction,LeaveZeroDecision
from app.leave_carry_allocation_store import review_carry_allocation,record_carry_allocation
from app.leave_carry_availability import read_carry_availability
from app.leave_zero_decision import review_zero_decision,record_zero_decision,_verify
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import full_year,FULL

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('mode',['carry','zero'])
def test_later_year_closing_preserves_net_prior_grant_attribution_and_retry(prior_year,monkeypatch,mode):
    db,args=full_year(prior_year,monkeypatch)
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    reason='Synthetic year2 closing'
    call=dict(source_selection=args,reason=reason)
    if mode=='carry':
        call.update(available_from=date(2027,1,2),available_through=date(2027,4,30))
        review=review_carry_allocation(db,**call)
        apply=record_carry_allocation
    else:
        review=review_zero_decision(db,**call)
        apply=record_zero_decision
    plan=review['plan'];assert plan['source']['version']=='year2-recorded-v1'
    assert Fraction(plan['source']['attributed_amount'])==Fraction(9,7)
    actual=Fraction(plan.get('carried_amount',plan.get('zeroed_amount')))
    assert actual==Fraction(plan['source']['net_amount'])
    confirm=dict(operation_id='6'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    first=apply(db,**confirm);assert apply(db,**confirm)==first
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before+(mode=='zero')
    if mode=='carry':
        ident={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
        result=read_carry_availability(db,allocation_id='6'*64,employee_id=args['employee_id'],leave_type_id=args['leave_type_id'],work_date=date(2027,1,3),**ident)
        assert Fraction(result['source_amount'])==actual
        with pytest.raises(ValueError,match='already carried'):
            review_zero_decision(db,source_selection=args,reason=reason)
    else:
        row=db.get(LeaveZeroDecision,'6'*64);assert _verify(db,row)==first
        with pytest.raises(ValueError):
            review_carry_allocation(db,source_selection=args,available_from=date(2027,1,2),available_through=date(2027,4,30),reason=reason)
