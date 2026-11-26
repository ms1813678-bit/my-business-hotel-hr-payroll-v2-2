from datetime import date,timedelta
from fractions import Fraction
import pytest
from sqlalchemy import select,func
from app.models import LeaveBalanceTransaction,LeaveRegisterEntry
from app.leave_carry_consumption import review_carry_consumption,record_carry_consumption
from app.leave_carry_expiry import review_carry_expiry
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_carry_consumption import ready
from tests.test_leave_next_year_earning import FULL

def prepared_expiry(prior_year,category):
    db,usage=ready(prior_year,category=category)
    from app.leave_month_earning import review_month_earning,apply_month_earning
    from app.models import LeaveMonthEarning
    month_receipt=db.get(LeaveMonthEarning,usage['monthly_receipt_id'])
    auth={k:usage[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    for month in (2,3,4):
        day=date(2026,month,1)
        while day.month==month:
            db.add(LeaveRegisterEntry(employee_id=usage['employee_id'],work_date=day,code='',source='synthetic',recorded_by='synthetic'))
            day+=timedelta(days=1)
        db.flush()
        first=date(2026,month,1)
        call=dict(selection_id=month_receipt.selection_id,month_start=first,evaluated_on=date(2026,5,1),
            current_insurance_years={(usage['employee_id'],date(2026,m,1)):None for m in range(1,month+1)},age_evaluation_dates={date(2026,m,1):date(2026,m,1) for m in range(1,month+1)},**auth)
        result=review_month_earning(db,**call)
        apply_month_earning(db,operation_id=str(month+4)*64,reviewed_digest=result['reviewed_digest'],human_confirmed=True,reason='Synthetic full expiry coverage',**call)
    usage['quantity']=Fraction(2,7)
    reviewed=review_carry_consumption(db,**usage)
    record_carry_consumption(db,operation_id='e'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**usage)
    call={k:usage[k] for k in ('grant_kind','grant_id','employee_id','leave_type_id','actor_id','session_id','session_version','expected_epoch')}
    call.update(evaluated_on=date(2026,5,1),reason='Synthetic explicit expired grant exclusion')
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    with pytest.raises(ValueError,match='Unallocated register usage'):
        review_carry_expiry(db,**call)
    # Fully attribute each actual usage day before subtracting any remainder.
    extra=[(3,Fraction(5,7)),(10,Fraction(1)),(17,Fraction(1))] if category=='W' else [(3,Fraction(5,7))]
    for n,(day,quantity) in enumerate(extra):
        part={**usage,'work_date':date(2026,1,day),'quantity':quantity}
        reviewed=review_carry_consumption(db,**part)
        record_carry_consumption(db,operation_id=str(n+1)*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**part)
    result=review_carry_expiry(db,**call)
    assert Fraction(result['plan']['consumed'])==(3 if category=='W' else 1)
    assert Fraction(result['plan']['excluded'])==Fraction(result['plan']['grant']['ceiling'])-(3 if category=='W' else 1)
    assert Fraction(result['plan']['proposed_ledger_delta'])==-Fraction(result['plan']['excluded'])
    assert result['posting_allowed'] is False
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    with pytest.raises(ValueError,match='remains available'):
        review_carry_expiry(db,**{**call,'evaluated_on':date(2026,4,30)})
    return db,call,usage,result


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('category',['W','A'])
def test_exact_expiry_posts_once_no_cash_then_cash_and_next_month_use_verified_net(prior_year,category):
    from app.leave_carry_expiry import apply_carry_expiry,verify_expiry_receipt
    from app.leave_cash_out import verified_released_balance,review_cash_out
    from app.models import LeaveCarryExpiry
    from tests.test_leave_cash_out import payroll
    from app.leave_month_earning import review_month_earning,apply_month_earning
    db,call,usage,review=prepared_expiry(prior_year,category)
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    confirm=dict(operation_id='f'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    first=apply_carry_expiry(db,**confirm)
    assert apply_carry_expiry(db,**confirm)==first
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before+1
    assert first['cash_payment_committed'] is False
    row=db.get(LeaveCarryExpiry,'f'*64)
    assert verify_expiry_receipt(db,row)==first
    with pytest.raises(ValueError,match='already has'):
        review_carry_expiry(db,**call)
    with pytest.raises(ValueError,match='recorded expiry'):
        review_carry_consumption(db,**usage)
    auth={k:call[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    source=verified_released_balance(db,employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],identity=auth)
    assert source['balance']>0
    _run,item=payroll(db,prior_year[1],start=date(2026,5,1),end=date(2026,5,31),status='draft')
    cash=review_cash_out(db,employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],
        days=Fraction(1,4),payroll_item_id=item.id,evaluated_on=date(2026,5,31),**auth)
    assert cash['reviewed_digest'] and cash['plan']['days']=='1/4'
    day=date(2026,5,1)
    while day.month==5:
        db.add(LeaveRegisterEntry(employee_id=call['employee_id'],work_date=day,code='',source='synthetic',recorded_by='synthetic'))
        day+=timedelta(days=1)
    db.flush()
    monthly=dict(selection_id=prior_year[3]['selection_id'],month_start=date(2026,5,1),evaluated_on=date(2026,6,1),
        current_insurance_years={(call['employee_id'],date(2026,m,1)):None for m in range(1,6)},
        age_evaluation_dates={date(2026,m,1):date(2026,m,1) for m in range(1,6)},**auth)
    reviewed=review_month_earning(db,**monthly)
    apply_month_earning(db,operation_id='0'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,
        reason='Synthetic post exclusion earning',**monthly)
    assert apply_carry_expiry(db,**confirm)==first


@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
@pytest.mark.parametrize('mode',['audit_failure','changed_register','changed_month','changed_audit','immutable','role','epoch','unconfirmed','different_retry','missing_window_month','later_legacy_debit','missing_grant_audit'])
def test_expiry_atomicity_fresh_sources_immutable_receipt_and_authority(prior_year,mode,monkeypatch):
    from app.leave_carry_expiry import apply_carry_expiry,verify_expiry_receipt
    from app.models import LeaveCarryExpiry,LeaveMonthEarning,AuditLog,AdminAccount
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    db,call,usage,review=prepared_expiry(prior_year,'A')
    confirm=dict(operation_id='f'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
    if mode=='audit_failure':
        original=db.flush
        def fail(*a,**kw):
            if any(isinstance(row,AuditLog) and row.action=='leave_carry_expired' for row in db.new):
                raise RuntimeError('Synthetic expiry audit failure')
            return original(*a,**kw)
        monkeypatch.setattr(db,'flush',fail)
        with pytest.raises(RuntimeError):apply_carry_expiry(db,**confirm)
        monkeypatch.setattr(db,'flush',original)
        assert db.scalar(select(func.count()).select_from(LeaveCarryExpiry))==0
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(
            LeaveBalanceTransaction.source_operation_key=='leave_carry_expiry:'+confirm['operation_id']))==0
        return
    if mode=='missing_window_month':
        db.execute(text('ALTER TABLE leave_month_earnings DISABLE TRIGGER USER'))
        db.execute(text("DELETE FROM leave_month_earnings WHERE month_start='2026-04-01'"))
        db.execute(text('ALTER TABLE leave_month_earnings ENABLE TRIGGER USER'));db.expire_all()
    elif mode=='later_legacy_debit':
        from decimal import Decimal
        db.add(LeaveBalanceTransaction(employee_id=call['employee_id'],leave_type_id=call['leave_type_id'],
            transaction_type='adjustment',days_delta=Decimal('-0.1'),created_by='synthetic',reason='Unallocated old debit',earning_year=2026,source_operation_key='unknown:synthetic',source_digest='a'*64,released_on=date(2026,5,1)))
        db.flush()
    elif mode=='missing_grant_audit':
        db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
        db.execute(text("DELETE FROM audit_logs WHERE action='leave_annual_settled'"))
        db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER'));db.expire_all()
    elif mode=='changed_register':
        entry=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==call['employee_id'],
            LeaveRegisterEntry.work_date==date(2026,4,15)))
        entry.code='A';db.flush()
    elif mode=='role':db.get(AdminAccount,call['actor_id']).role='assistant_manager';db.flush()
    elif mode=='epoch':confirm['expected_epoch']='stale-epoch'
    elif mode=='unconfirmed':confirm['human_confirmed']=False
    elif mode in ('changed_month','changed_audit','immutable','different_retry'):
        apply_carry_expiry(db,**confirm)
        if mode=='immutable':
            for sql in ('UPDATE leave_carry_expiries SET excluded_canonical=excluded_canonical',
                        'DELETE FROM leave_carry_expiries','TRUNCATE leave_carry_expiries'):
                with pytest.raises(DBAPIError):
                    with db.begin_nested():db.execute(text(sql))
            return
        if mode=='different_retry':confirm['reason']='Changed retry'
        if mode=='changed_month':
            db.execute(text('ALTER TABLE leave_month_earnings DISABLE TRIGGER USER'))
            db.execute(text("UPDATE leave_month_earnings SET source_digest=repeat('0',64) WHERE month_start='2026-04-01'"))
            db.execute(text('ALTER TABLE leave_month_earnings ENABLE TRIGGER USER'));db.expire_all()
        if mode=='changed_audit':
            db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
            db.execute(text("UPDATE audit_logs SET reason='changed' WHERE action='leave_carry_expired'"))
            db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER'));db.expire_all()
    with pytest.raises(ValueError):apply_carry_expiry(db,**confirm)
