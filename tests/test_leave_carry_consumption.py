from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select,func
from app.models import LeaveCarryAllocation,LeaveBalanceTransaction,LeaveMonthEarning,LeaveCarryConsumption
from app.leave_month_earning import review_month_earning,apply_month_earning
from app.leave_carry_consumption import review_carry_consumption,record_carry_consumption
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_next_year_earning import next_month,FULL


def ready(prior_year,category='W'):
    db,call=next_month(prior_year)
    if category=='A':
        from app.models import LeaveRegisterEntry
        entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==prior_year[1].id,LeaveRegisterEntry.work_date==date(2026,1,3)))
        entry.code='A';db.flush()
    reviewed=review_month_earning(db,**call)
    apply_month_earning(db,operation_id='b'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,reason='Synthetic',**call)
    employee=prior_year[1];kind=prior_year[2][category]
    grant=db.scalar(select(LeaveCarryAllocation).where(LeaveCarryAllocation.employee_id==employee.id,LeaveCarryAllocation.leave_type_id==kind))
    if category=='A':
        from app.models import LeaveAnnualSettlement
        grant=db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.employee_id==employee.id))
    month=db.get(LeaveMonthEarning,'b'*64)
    import json
    usage=next(item for item in json.loads(month.result_json)['ledger'] if item['category']==category and item['kind']=='usage')
    auth={k:call[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    selection=dict(grant_kind='annual' if category=='A' else 'carry',grant_id=grant.id,employee_id=employee.id,leave_type_id=kind,
        monthly_receipt_id=month.id,usage_ledger_id=usage['id'],work_date=date(2026,1,3),quantity=Fraction(1),reason='Synthetic selected grant',**auth)
    return db,selection


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_actual_next_year_usage_attribution_no_second_debit_and_exact_fraction_split(prior_year):
    db,call=ready(prior_year)
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    reviewed=review_carry_consumption(db,**call)
    confirm=dict(operation_id='a'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**call)
    first=record_carry_consumption(db,**confirm)
    assert first==record_carry_consumption(db,**confirm)
    assert first['ledger_delta']=='0/1' and first['plan']['grant_attributed_after']=='1/1'
    with pytest.raises(ValueError,match='Register day already'):
        review_carry_consumption(db,**call)
    call={**call,'work_date':date(2026,1,10),'quantity':Fraction(2,7)}
    review=review_carry_consumption(db,**call)
    record_carry_consumption(db,operation_id='c'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    assert record_carry_consumption(db,**confirm)==first
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    assert db.scalar(select(func.count()).select_from(LeaveCarryConsumption))==2


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('mode',['wrong_day','wrong_type','wrong_employee','over_day','zero','unconfirmed','wrong_month'])
def test_invalid_usage_or_identity_never_creates_attribution(prior_year,mode):
    db,call=ready(prior_year)
    if mode=='wrong_day':call['work_date']=date(2026,1,4)
    if mode=='wrong_type':call['leave_type_id']=prior_year[2]['A']
    if mode=='wrong_employee':call['employee_id']+=10000
    if mode=='over_day':call['quantity']=Fraction(8,7)
    if mode=='zero':call['quantity']=Fraction(0)
    if mode=='wrong_month':call['work_date']=date(2026,5,3)
    with pytest.raises(ValueError):
        review=review_carry_consumption(db,**call)
        record_carry_consumption(db,operation_id='a'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=mode!='unconfirmed',**call)
    assert db.scalar(select(func.count()).select_from(LeaveCarryConsumption))==0


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_annual_settlement_grant_consumes_actual_next_year_annual_usage(prior_year):
    db,call=ready(prior_year,category='A')
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    review=review_carry_consumption(db,**call)
    assert review['plan']['grant']['ceiling']=='3/2'
    saved=record_carry_consumption(db,operation_id='a'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    assert saved['plan']['grant_unattributed_ceiling_after']=='1/2'
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('mode',['audit_failure','register_changed','ledger_changed','audit_changed','immutable'])
def test_consumption_atomic_evidence_and_immutable_receipt(prior_year,mode,monkeypatch):
    from sqlalchemy import text
    from app.models import AuditLog,LeaveRegisterEntry
    from sqlalchemy.exc import DBAPIError
    db,call=ready(prior_year)
    reviewed=review_carry_consumption(db,**call)
    confirm=dict(operation_id='a'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**call)
    if mode=='audit_failure':
        original=db.flush
        def fail(*a,**kw):
            if any(isinstance(row,AuditLog) and row.action=='leave_carry_consumed' for row in db.new):raise RuntimeError('Synthetic audit failure')
            return original(*a,**kw)
        monkeypatch.setattr(db,'flush',fail)
        with pytest.raises(RuntimeError):record_carry_consumption(db,**confirm)
        monkeypatch.setattr(db,'flush',original)
        assert db.scalar(select(func.count()).select_from(LeaveCarryConsumption))==0
        return
    record_carry_consumption(db,**confirm)
    if mode=='immutable':
        for sql in ['UPDATE leave_carry_consumptions SET quantity_canonical=quantity_canonical','DELETE FROM leave_carry_consumptions','TRUNCATE leave_carry_consumptions']:
            with pytest.raises(DBAPIError):
                with db.begin_nested():db.execute(text(sql))
        return
    if mode=='register_changed':
        entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==call['employee_id'],LeaveRegisterEntry.work_date==call['work_date']))
        entry.code='A';db.flush()
    if mode=='ledger_changed':
        db.execute(text('ALTER TABLE leave_balance_transactions DISABLE TRIGGER USER'))
        db.execute(text('UPDATE leave_balance_transactions SET days_delta=-2 WHERE id=:i'),{'i':call['usage_ledger_id']})
        db.execute(text('ALTER TABLE leave_balance_transactions ENABLE TRIGGER USER'));db.expire_all()
    if mode=='audit_changed':
        db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
        db.execute(text("UPDATE audit_logs SET reason='forged' WHERE action='leave_carry_consumed'"))
        db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER'));db.expire_all()
    with pytest.raises(ValueError):record_carry_consumption(db,**confirm)


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_manual_cash_refuses_expired_unresolved_carry_not_ordinary_positive_balance(prior_year,monkeypatch):
    from app.leave_cash_out import review_cash_out
    from tests.test_leave_cash_out import payroll
    db,call=ready(prior_year,category='A')
    _run,item=payroll(db,prior_year[1],start=date(2026,1,1),end=date(2026,1,31),status='draft')
    auth={k:call[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    _freeze_cash_clock(monkeypatch,date(2026,4,30))
    # Same verified ledger has ample positive current-year credit. Before the
    # recorded window closes it is not the expired-grant condition.
    review_cash_out(db,employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],
        days=Fraction(1,4),payroll_item_id=item.id,evaluated_on=date(2026,4,30),**auth)
    _freeze_cash_clock(monkeypatch,date(2026,5,1))
    with pytest.raises(ValueError,match='Expired carry remains unresolved'):
        review_cash_out(db,employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],
            days=Fraction(1,4),payroll_item_id=item.id,evaluated_on=date(2026,5,1),**auth)


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_annual_fractional_grant_ceiling_cannot_be_overspent_on_another_valid_day(prior_year):
    from app.models import LeaveRegisterEntry
    from tests.test_leave_next_year_earning import next_month
    db,monthcall=next_month(prior_year)
    for day in (3,10):
        entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==prior_year[1].id,LeaveRegisterEntry.work_date==date(2026,1,day)))
        entry.code='A'
    db.flush()
    review=review_month_earning(db,**monthcall)
    apply_month_earning(db,operation_id='b'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic',**monthcall)
    from app.models import LeaveAnnualSettlement
    grant=db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.employee_id==prior_year[1].id))
    import json
    receipt=db.get(LeaveMonthEarning,'b'*64)
    usage=next(item for item in json.loads(receipt.result_json)['ledger'] if item['category']=='A' and item['kind']=='usage')
    auth={k:monthcall[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    call=dict(grant_kind='annual',grant_id=grant.id,employee_id=prior_year[1].id,leave_type_id=prior_year[2]['A'],
        monthly_receipt_id=receipt.id,usage_ledger_id=usage['id'],work_date=date(2026,1,3),quantity=Fraction(1),reason='Synthetic',**auth)
    review=review_carry_consumption(db,**call)
    record_carry_consumption(db,operation_id='a'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    with pytest.raises(ValueError,match='remaining grant ceiling'):
        review_carry_consumption(db,**{**call,'work_date':date(2026,1,10),'quantity':Fraction(3,4)})
    call={**call,'work_date':date(2026,1,10),'quantity':Fraction(1,2)}
    review=review_carry_consumption(db,**call)
    saved=record_carry_consumption(db,operation_id='c'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    assert saved['plan']['grant_unattributed_ceiling_after']=='0/1'


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_later_unallocated_manual_cash_cannot_also_be_assigned_to_old_grant(prior_year,monkeypatch):
    from app.leave_cash_out import review_cash_out,apply_cash_out
    from tests.test_leave_cash_out import payroll
    db,call=ready(prior_year,category='A')
    _run,item=payroll(db,prior_year[1],start=date(2026,1,1),end=date(2026,1,31),status='draft')
    auth={k:call[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    _freeze_cash_clock(monkeypatch,date(2026,1,31))
    cash=dict(employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],days=Fraction(1,2),
        payroll_item_id=item.id,evaluated_on=date(2026,1,31),**auth)
    review=review_cash_out(db,**cash)
    apply_cash_out(db,operation_id='e'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,reason='Synthetic manual cash',**cash)
    with pytest.raises(ValueError,match='Later cash-out lacks grant allocation'):
        review_carry_consumption(db,**call)



def _freeze_cash_clock(monkeypatch,day):
    from datetime import datetime
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.combine(day,datetime.min.time(),tzinfo=tz)
    monkeypatch.setattr('app.leave_carry_consumption.datetime',Clock)


@pytest.mark.parametrize('prior_year',[{'hire_date':date(2025,11,25)}],indirect=True)
def test_late_hire_annual_grant_is_available_anytime_within_next_year(prior_year):
    from app.leave_annual_settlement_store import review_annual_settlement_apply,apply_annual_settlement
    from app.leave_carry_consumption import _grant
    db,employee,kinds,args=prior_year
    call=dict(source_selection={**args,'leave_type_id':kinds['A']},full_annual_entitlement=15,payroll_item_id=None,
        available_from=date(2026,1,1),available_through=date(2026,1,31),reason='Synthetic late hire preserve')
    review=review_annual_settlement_apply(db,**call)
    saved=apply_annual_settlement(db,operation_id='f'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    assert saved['cash_result'] is None
    auth={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    grant=dict(grant_kind='annual',grant_id='f'*64,employee_id=employee.id,leave_type_id=kinds['A'],identity=auth)
    assert _grant(db,work_date=date(2026,1,1),**grant)['available_through']=='2026-12-31'
    assert _grant(db,work_date=date(2026,12,31),**grant)['available_through']=='2026-12-31'
    for invalid in (date(2025,12,31),date(2027,1,1)):
        with pytest.raises(ValueError,match='unavailable'):_grant(db,work_date=invalid,**grant)


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_normal_annual_grant_window_includes_both_ends_and_excludes_adjacent_days(prior_year):
    from app.leave_carry_consumption import _grant
    db,call=ready(prior_year,category='A')
    auth={k:call[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    grant=dict(grant_kind='annual',grant_id=call['grant_id'],employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],identity=auth)
    for valid in (date(2026,1,1),date(2026,4,30)):assert _grant(db,work_date=valid,**grant)['ceiling']=='3/2'
    for invalid in (date(2025,12,31),date(2026,5,1)):
        with pytest.raises(ValueError,match='unavailable'):_grant(db,work_date=invalid,**grant)
