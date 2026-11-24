from datetime import date
from decimal import Decimal
from sqlalchemy import select,func,inspect
import pytest
from app.pay_basis_source import preview_stored_pay_basis
from app.models import PayrollRun,PayrollItem,Employee,AuditLog
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def setup(history_context):
    db,actor,auth,fact=history_context
    employee=db.get(Employee,fact.employee_id)
    run=PayrollRun(period_start=date(2026,2,1),period_end=date(2026,2,28),pay_date=date(2026,3,1),status='draft',created_by='synthetic')
    db.add(run);db.flush()
    item=PayrollItem(payroll_run_id=run.id,employee_id=employee.id,employee_code_snapshot=employee.code,employee_name_snapshot='Synthetic saved name',department_name_snapshot='Synthetic department',position_snapshot='Synthetic',salary_basis=Decimal(6000),base_salary=Decimal(3000),daily_hours=Decimal(8),performance_bonus=Decimal(3000),attendance_bonus=Decimal(100),occupancy_bonus=Decimal(200),other_bonus=Decimal(300),staff_reward_amount=Decimal(400),net_pay=Decimal(0),overtime_amount=Decimal(0))
    db.add(item);db.flush()
    return db,auth,item,run


def test_actual_item_source_salary_not_prorated_base_no_write(history_context):
    db,auth,item,run=setup(history_context)
    args=dict(item_id=item.id,cashout_days=Decimal(3),overtime_divisor_hours=8,manual_overtime_hours=Decimal(2),**auth)
    def snapshot():return tuple(getattr(item,c.key) for c in inspect(PayrollItem).columns)
    before=snapshot();audit=db.scalar(select(func.count()).select_from(AuditLog))
    r=preview_stored_pay_basis(db,**args)
    assert r.policy_preview.special_basis==9000 and r.policy_preview.cashout_amount_unrounded==900
    assert r.policy_preview.overtime_amount_unrounded==75 and r.stored_payable_base==3000
    assert r.employee_name=='Synthetic saved name' and not r.posting_allowed
    assert snapshot()==before and db.scalar(select(func.count()).select_from(AuditLog))==audit
    item.performance_bonus=Decimal(1500);db.flush()
    changed=preview_stored_pay_basis(db,**args)
    assert r.source_digest!=changed.source_digest and changed.policy_preview.special_basis==7500


@pytest.mark.parametrize('change',['epoch','session','finance','wrongperiod','missingitem'])
def test_adapter_current_identity_and_period_refusal(history_context,change):
    db,auth,item,run=setup(history_context)
    args=dict(item_id=item.id,cashout_days=Decimal(3),overtime_divisor_hours=8,manual_overtime_hours=Decimal(2),**auth)
    if change=='epoch':args['expected_epoch']='stale'
    elif change=='session':args['session_version']+=1
    elif change=='finance':
        from app.models import AdminAccount
        db.get(AdminAccount,auth['actor_id']).role='finance'
    elif change=='wrongperiod':run.period_end=date(2026,2,27)
    else:args['item_id']=2147483647
    db.flush()
    with pytest.raises(ValueError):preview_stored_pay_basis(db,**args)
