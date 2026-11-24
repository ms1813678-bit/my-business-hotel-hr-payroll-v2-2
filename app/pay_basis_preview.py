"""Owner policy pay-basis what-if only. Never changes payroll or awards money.

Nov23 user rules: ordinary salary uses actual calendar month days; manual leave
cashout and OT use basic plus selected-month performance only, divisor30. OT
8/9/12 are explicit rate divisors, not rosters or automatically approved hours.
"""
from calendar import monthrange
from dataclasses import dataclass,field
from datetime import date
from decimal import Decimal,localcontext
from hashlib import sha256
import json
from app.leave_balance_rules import _bounded_amount


@dataclass(frozen=True)
class PayBasisPreview:
    month_start:date
    ordinary_month_days:int
    ordinary_basic_daily:Decimal
    special_basis:Decimal
    cashout_daily:Decimal
    cashout_days:Decimal
    cashout_amount_unrounded:Decimal
    overtime_divisor_hours:int
    overtime_hourly:Decimal
    manual_overtime_hours:Decimal
    overtime_amount_unrounded:Decimal
    excluded_bonus_total:Decimal
    source_digest:str
    establishes_cashout_eligibility:bool=field(default=False,init=False)
    establishes_overtime_approval:bool=field(default=False,init=False)
    posting_allowed:bool=field(default=False,init=False)


def preview_pay_basis(*,month_start,basic_salary,performance_bonus,attendance_bonus,occupancy_bonus,other_bonus,
        staff_reward_amount,cashout_days,overtime_divisor_hours,manual_overtime_hours):
    """Explicit synthetic/caller-sourced values, no current employee lookup.

    No performance award is inferred from its maximum or an evaluation score.
    Cashout days are a hypothetical request, not proof of eligible leave days.
    Existing payroll calculations remain unchanged. No premium is invented.
    """
    if type(month_start) is not date or month_start.day!=1:raise ValueError('Explicit first-of-month required.')
    if type(overtime_divisor_hours) is not int or overtime_divisor_hours not in (8,9,12):raise ValueError('Explicit rate divisor required.')
    amounts=dict(basic_salary=basic_salary,performance_bonus=performance_bonus,attendance_bonus=attendance_bonus,
        occupancy_bonus=occupancy_bonus,other_bonus=other_bonus,staff_reward_amount=staff_reward_amount,
        cashout_days=cashout_days,manual_overtime_hours=manual_overtime_hours)
    for amount in amounts.values():
        _bounded_amount(amount)
        if amount<0:raise ValueError('Nonnegative explicit preview amounts required.')
        if len(amount.as_tuple().digits)>100 or abs(amount.as_tuple().exponent)>100:raise ValueError('Bounded preview precision required.')
    days=monthrange(month_start.year,month_start.month)[1]
    with localcontext() as context:
        context.prec=250
        basis=basic_salary+performance_bonus
        ordinary=basic_salary/Decimal(days)
        daily=basis/Decimal(30)
        hourly=daily/Decimal(overtime_divisor_hours)
        cash=daily*cashout_days
        overtime=hourly*manual_overtime_hours
        excluded=attendance_bonus+occupancy_bonus+other_bonus+staff_reward_amount
    payload=dict(month_start=month_start.isoformat(),overtime_divisor_hours=overtime_divisor_hours,
        **{k:format(v,'f') for k,v in amounts.items()})
    digest=sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return PayBasisPreview(month_start,days,ordinary,basis,daily,cashout_days,cash,overtime_divisor_hours,
        hourly,manual_overtime_hours,overtime,excluded,digest)
