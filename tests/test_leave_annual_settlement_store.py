from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, func
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement
from app.leave_workflow import leave_balance
from app.models import LeaveCashOut, LeaveAnnualSettlement, AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_cash_out import payroll


@pytest.mark.parametrize('prior_year,cash,carry', [
    ({'hire_date':date(2025,1,1),'used_annual':9},'6/1','0/1'),
    ({'hire_date':date(2025,1,1),'used_annual':6},'15/2','3/2'),
],indirect=['prior_year'])
def test_actual_annual_cash_debit_obligation_and_carry_remainder_atomic(prior_year,cash,carry):
    db,employee,kinds,args=prior_year
    run,item=payroll(db,employee,start=date(2025,12,1),end=date(2025,12,31),status='draft')
    selection=dict(source_selection={**args,'leave_type_id':kinds['A']},full_annual_entitlement=15,
        payroll_item_id=item.id,available_from=date(2026,1,1) if Fraction(carry)>0 else None,
        available_through=date(2026,4,30) if Fraction(carry)>0 else None,reason='Synthetic actual annual close')
    reviewed=review_annual_settlement_apply(db,**selection)
    assert reviewed['plan']['annual']['cash_days']==cash
    before=leave_balance(db,employee.id,kinds['A'])
    confirm=dict(operation_id='f'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**selection)
    result=apply_annual_settlement(db,**confirm)
    assert Fraction(result['carry_days'])==Fraction(carry)
    assert leave_balance(db,employee.id,kinds['A'])==Fraction(carry)
    assert Fraction(before)-Fraction(cash)==Fraction(carry)
    from app.leave_carry_source import review_carry_source
    auth = {k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    year = review_carry_source(db,employee_id=employee.id,leave_type_id=kinds['A'],source_year=2025,reviewed_on=args['reviewed_on'],**auth)
    assert year['amount'] == Fraction(carry)
    receipt = db.get(LeaveCashOut,'f'*64)
    ledger = db.get(__import__('app.models',fromlist=['LeaveBalanceTransaction']).LeaveBalanceTransaction,receipt.ledger_id)
    assert ledger.earning_year == 2025 and receipt.evaluated_on.year == 2026
    assert not result['cash_payment_committed'] and result['carry_duplicate_credit']=='0/1'
    assert db.scalar(select(func.count()).select_from(LeaveCashOut))==1
    assert db.scalar(select(func.count()).select_from(LeaveAnnualSettlement))==1
    assert apply_annual_settlement(db,**confirm)==result
    with pytest.raises(ValueError,match='already settled'):
        apply_annual_settlement(db,**{**confirm,'operation_id':'e'*64})


@pytest.mark.parametrize('prior_year',[{'hire_date':date(2025,1,1),'used_annual':6}],indirect=True)
def test_annual_audit_failure_rolls_back_cash_debit_obligation_and_decision(prior_year,monkeypatch):
    db,employee,kinds,args=prior_year
    run,item=payroll(db,employee,start=date(2025,12,1),end=date(2025,12,31),status='draft')
    selection=dict(source_selection={**args,'leave_type_id':kinds['A']},full_annual_entitlement=15,
        payroll_item_id=item.id,available_from=date(2026,1,1),available_through=date(2026,4,30),reason='Synthetic rollback')
    reviewed=review_annual_settlement_apply(db,**selection); before=leave_balance(db,employee.id,kinds['A'])
    real_flush=db.flush
    def failed(*a,**kw):
        if any(isinstance(row,AuditLog) and row.action=='leave_annual_settled' for row in db.new):
            raise RuntimeError('Synthetic annual audit failure')
        return real_flush(*a,**kw)
    monkeypatch.setattr(db,'flush',failed)
    with pytest.raises(RuntimeError,match='annual audit failure'):
        apply_annual_settlement(db,operation_id='f'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**selection)
    monkeypatch.setattr(db,'flush',real_flush)
    assert leave_balance(db,employee.id,kinds['A'])==before
    assert db.scalar(select(func.count()).select_from(LeaveCashOut))==0
    assert db.scalar(select(func.count()).select_from(LeaveAnnualSettlement))==0
