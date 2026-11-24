"""Locked source-bound what-if adapter for owner pay-basis policy. No posting."""
from dataclasses import dataclass,field
from datetime import date
from hashlib import sha256
import json
from sqlalchemy import select,text
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_policy_store import policy_read_guard
from app.pay_basis_preview import preview_pay_basis
from app.models import PayrollItem,PayrollRun


@dataclass(frozen=True)
class PayrollBasisComparison:
    item_id:int
    run_id:int
    employee_id:int
    employee_name:str
    employee_code:str
    current_run_status:str
    stored_salary_basis:object
    stored_payable_base:object
    stored_overtime_amount:object
    policy_preview:object
    source_digest:str
    issues:tuple
    posting_allowed:bool=field(default=False,init=False)


@bounded_workbook_operation
def preview_stored_pay_basis(db,*,item_id,cashout_days,overtime_divisor_hours,manual_overtime_hours,**identity):
    """HR current session only, exact item/run. Saved period values not currentpay.

    salary_basis is the saved full-month salary basis, not prorated base_salary.
    performance_bonus is the saved selected-period amount. No maximum award or
    manager score is invented. Preview does not modify existing live formulas.
    """
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read-only transaction required.')
    policy_read_guard(db,**identity)
    if type(item_id) is not int or item_id<1:raise ValueError('Explicit payroll item required.')
    db.execute(text('LOCK TABLE payroll_items,payroll_runs IN SHARE MODE'))
    policy_read_guard(db,**identity)
    item=db.scalar(select(PayrollItem).where(PayrollItem.id==item_id).execution_options(populate_existing=True))
    if item is None:raise ValueError('Payroll item unavailable.')
    run=db.get(PayrollRun,item.payroll_run_id,populate_existing=True)
    if run is None or type(run.period_start) is not date or type(run.period_end) is not date or run.period_start.day!=1:
        raise ValueError('Explicit complete payroll period required.')
    from calendar import monthrange
    if run.period_end!=date(run.period_start.year,run.period_start.month,monthrange(run.period_start.year,run.period_start.month)[1]):
        raise ValueError('Complete calendar month payroll period required.')
    result=preview_pay_basis(month_start=run.period_start,basic_salary=item.salary_basis,performance_bonus=item.performance_bonus,
        attendance_bonus=item.attendance_bonus,occupancy_bonus=item.occupancy_bonus,other_bonus=item.other_bonus,
        staff_reward_amount=item.staff_reward_amount,cashout_days=cashout_days,overtime_divisor_hours=overtime_divisor_hours,
        manual_overtime_hours=manual_overtime_hours)
    from sqlalchemy import inspect
    def serial(value):
        if value is None or type(value) in (str,int,bool):return value
        return str(value)
    source={kind:{c.key:serial(getattr(row,c.key)) for c in inspect(type(row)).columns} for kind,row in [('item',item),('run',run)]}
    source.update(policy_preview_digest=result.source_digest,restore_epoch=identity['expected_epoch'])
    digest=sha256(json.dumps(source,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return PayrollBasisComparison(item.id,run.id,item.employee_id,item.employee_name_snapshot,item.employee_code_snapshot,
        run.status,item.salary_basis,item.base_salary,item.overtime_amount,result,digest,
        ('saved_payroll_values_not_current_employee_salary','cashout_days_hypothetical_not_eligibility','manual_overtime_not_attendance_reviewed','existing_payroll_unchanged'))
