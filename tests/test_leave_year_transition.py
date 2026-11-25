from datetime import date
import pytest
from sqlalchemy import select, func, text
from app.models import LeaveBalanceTransaction, LeaveYearTransition, AuditLog
from app.leave_year_transition import review_year_transition,record_year_transition
from app.leave_carry_allocation_store import review_carry_allocation,record_carry_allocation
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_l8_source_year_cash_binding import settle,FULL


def prepared(prior_year):
    db,employee,kinds,item,auth=settle(prior_year)
    args=prior_year[3];sources={c:{**args,'leave_type_id':kinds[c]} for c in kinds}
    for index,code in enumerate(('W','H')):
        positive=code=='W'
        selection=dict(source_selection=sources[code],available_from=date(2026,1,1) if positive else None,
            available_through=date(2026,4,30) if positive else None,reason='Synthetic closing')
        review=review_carry_allocation(db,**selection)
        record_carry_allocation(db,operation_id=str(index+1)*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**selection)
    return db,sources


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_checkpoint_atomic_replay_preserves_ledger_and_has_no_earning_permission(prior_year):
    db,sources=prepared(prior_year)
    count=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    call=dict(source_selections=sources,reason='Synthetic transition checkpoint')
    review=review_year_transition(db,**call)
    assert review['plan']['balances']['A']['all_time_balance']=='3/2'
    assert review['plan']['balances']['W']['all_time_balance']=='365/7'
    confirm=dict(operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    result=record_year_transition(db,**confirm)
    assert result==record_year_transition(db,**confirm)
    assert not result['new_year_earning_allowed'] and not result['operational_spending_allowed']
    assert result['new_opening_credit']=='0/1' and result['ledger_delta']=='0/1'
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==count
    assert db.scalar(select(func.count()).select_from(LeaveYearTransition))==1
    with pytest.raises(ValueError,match='already has'):
        record_year_transition(db,**{**confirm,'operation_id':'e'*64})


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('mode',['missing','employee','year','category','unconfirmed','audit_failure'])
def test_checkpoint_missing_conflicting_or_unconfirmed_sources_refuse(prior_year,mode,monkeypatch):
    db,sources=prepared(prior_year)
    call=dict(source_selections=sources,reason='Synthetic')
    reviewed=review_year_transition(db,**call)
    if mode=='missing':sources.pop('W')
    if mode=='employee':sources['W']={**sources['W'],'employee_id':sources['W']['employee_id']+1}
    if mode=='year':sources['W']={**sources['W'],'source_year':2024}
    if mode=='category':sources['W']={**sources['W'],'leave_type_id':sources['A']['leave_type_id']}
    if mode=='audit_failure':
        original=db.flush
        def fail(*a,**kw):
            if any(isinstance(row,AuditLog) and row.action=='leave_year_transition_recorded' for row in db.new):raise RuntimeError('Synthetic audit failure')
            return original(*a,**kw)
        monkeypatch.setattr(db,'flush',fail)
    with pytest.raises((ValueError,RuntimeError)):
        record_year_transition(db,operation_id='d'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=mode!='unconfirmed',**call)
    assert db.scalar(select(func.count()).select_from(LeaveYearTransition))==0


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('sql',['UPDATE leave_year_transitions SET source_year=source_year','DELETE FROM leave_year_transitions','TRUNCATE leave_year_transitions'])
def test_checkpoint_receipt_is_immutable(prior_year,sql):
    from sqlalchemy.exc import DBAPIError
    db,sources=prepared(prior_year);call=dict(source_selections=sources,reason='Synthetic')
    review=review_year_transition(db,**call)
    record_year_transition(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    with pytest.raises(DBAPIError):
        with db.begin_nested():db.execute(text(sql))


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('mode',['missing_closing','stale_register','role','epoch','forged_record'])
def test_checkpoint_rechecks_closing_live_source_current_authority_and_audit(prior_year,mode):
    from app.models import LeaveCarryAllocation,LeaveRegisterEntry,AdminAccount
    db,sources=prepared(prior_year);call=dict(source_selections=sources,reason='Synthetic')
    review=review_year_transition(db,**call)
    confirm=dict(operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    if mode=='forged_record':
        record_year_transition(db,**confirm)
        db.execute(text('ALTER TABLE leave_year_transitions DISABLE TRIGGER USER'))
        db.execute(text("UPDATE leave_year_transitions SET result_digest=repeat('0',64)"))
        db.execute(text('ALTER TABLE leave_year_transitions ENABLE TRIGGER USER'));db.expire_all()
    elif mode=='missing_closing':
        db.execute(text('ALTER TABLE leave_carry_allocations DISABLE TRIGGER USER'))
        db.execute(text("DELETE FROM leave_carry_allocations WHERE id=:i"),{'i':'1'*64})
        db.execute(text('ALTER TABLE leave_carry_allocations ENABLE TRIGGER USER'));db.expire_all()
    elif mode=='stale_register':
        entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==sources['A']['employee_id'],
            LeaveRegisterEntry.work_date==date(2025,12,31)));entry.code='U';db.flush()
    elif mode=='role':
        db.get(AdminAccount,sources['A']['actor_id']).role='assistant_manager';db.flush()
    elif mode=='epoch':
        for selection in sources.values():selection['expected_epoch']='synthetic_changed_epoch'
    with pytest.raises(ValueError):record_year_transition(db,**confirm)


@pytest.mark.parametrize('prior_year',[
    {'hire_date':FULL,'used_annual':15},
    {'hire_date':FULL,'used_annual':16},
    {'hire_date':date(2025,11,25)},
],indirect=True)
def test_cashless_zero_debt_and_late_hire_checkpoint_preserve_same_ledger(prior_year):
    from fractions import Fraction
    from tests.test_leave_cash_out import payroll
    from app.leave_annual_settlement_store import review_annual_settlement_apply,apply_annual_settlement
    db,employee,kinds,args=prior_year
    _run,item=payroll(db,employee,start=date(2025,12,1),end=date(2025,12,31),status='draft')
    sources={c:{**args,'leave_type_id':kinds[c]} for c in kinds}
    from app.leave_annual_settlement_source import review_annual_settlement
    annual=review_annual_settlement(db,source_selection=sources['A'],full_annual_entitlement=15,other_half_action='carry')['plan']
    positive=Fraction(annual['carried_days'])>0
    selection=dict(source_selection=sources['A'],full_annual_entitlement=15,payroll_item_id=None,
        available_from=date(2026,1,1) if positive else None,available_through=date(2026,4,30) if positive else None,reason='Synthetic cashless close')
    review=review_annual_settlement_apply(db,**selection)
    result=apply_annual_settlement(db,operation_id='f'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**selection)
    assert result['cash_result'] is None
    for index,code in enumerate(('W','H')):
        selection=dict(source_selection=sources[code],available_from=date(2026,1,1) if code=='W' else None,
            available_through=date(2026,4,30) if code=='W' else None,reason='Synthetic close')
        review=review_carry_allocation(db,**selection)
        record_carry_allocation(db,operation_id=str(index+1)*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**selection)
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    call=dict(source_selections=sources,reason='Synthetic checkpoint');review=review_year_transition(db,**call)
    saved=record_year_transition(db,operation_id='d'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    assert saved['balances']['A']['year_amount']==annual['released_remaining']
    assert saved['balances']['A']['held_annual']==annual['held_remaining']
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
