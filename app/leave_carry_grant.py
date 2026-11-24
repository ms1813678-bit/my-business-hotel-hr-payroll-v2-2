"""Grant-chosen carry duration, no fixed January or expiry/cash action.

Pure validation only. A grant does not erase balances when its end arrives.
Caller must supply the confirmed grant start/end and explicitly selected days.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class CarryGrant:
    employee_id: int
    code: str
    days: Decimal
    granted_on: date
    available_from: date
    available_through: date
    reason: str

    def __post_init__(self):
        if type(self.employee_id) is not int or self.employee_id < 1:
            raise ValueError('Resolved employee identity required.')
        if self.code not in {'A', 'H', 'W'}:
            raise ValueError('Explicit supported day balance category required.')
        if not isinstance(self.days, Decimal) or not self.days.is_finite() or self.days <= 0:
            raise ValueError('Explicit finite positive Decimal grant days required.')
        if any(type(day) is not date for day in (
                self.granted_on, self.available_from, self.available_through)):
            raise ValueError('Explicit grant and availability dates required.')
        if not self.granted_on <= self.available_from <= self.available_through:
            raise ValueError('Grant dates must be in explicit chronological order.')
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError('Explicit grant reason required.')


def grant_window_contains(grant: CarryGrant, work_date: date) -> bool:
    if not isinstance(grant, CarryGrant) or type(work_date) is not date:
        raise ValueError('Explicit grant and work date required.')
    return grant.available_from <= work_date <= grant.available_through
