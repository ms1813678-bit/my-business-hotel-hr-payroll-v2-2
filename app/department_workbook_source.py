"""Read-only, unexposed workbook source facts. No balance projection or posting.

Explicit employee/window selection is supplied by a future authorized adapter.
Current ledger totals are NOT carry/accrual/available balances for that window.
The digest is freshness evidence only, not proof of a chosen policy or grant.
"""
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from hashlib import sha256

from sqlalchemy import String, cast, select, text

from app.department_workbook_guard import bounded_workbook_operation
from app.leave_day_calculations import employed_dates
from app.models import (
    Department,
    Employee,
    EmployeeServicePeriod,
    LeaveBalanceTransaction,
    LeaveHoliday,
    LeaveMonthClose,
    LeaveRegisterEntry,
    LeaveType,
)
from app.restore_lock import acquire_credential_write_lock


@dataclass(frozen=True)
class WorkbookSourceFacts:
    canonical_json: str
    digest: str
    employed_dates: tuple[tuple[int, frozenset[date]], ...]
    missing_service_employee_ids: tuple[int, ...]


@bounded_workbook_operation
def read_workbook_source(db, *, department_id, employee_ids, window_start,
                         month_start, policy_digest, expected_epoch):
    """Hold restore and SHARE table locks until caller transaction ends.

    SHARE blocks normal DML, including inserts absent from earlier row sets.
    This is intentionally coarse offline preparation, not a deployed fast path.
    Caller must call BEFORE source-dependent reads and must not commit between
    this snapshot and binding/preparation. PostgreSQL READ COMMITTED required;
    no SQLite locking equivalence is claimed. No automatic retry on lock timeout.
    """
    if db.get_bind().dialect.name != 'postgresql':
        raise RuntimeError('Workbook source snapshots require PostgreSQL locking.')
    if (type(department_id) is not int or department_id < 1 or type(employee_ids) is not tuple
            or not 1 <= len(employee_ids) <= 300
            or any(type(i) is not int or i < 1 for i in employee_ids)
            or len(set(employee_ids)) != len(employee_ids)):
        raise ValueError('Explicit bounded unique employee selection required.')
    if (type(month_start) is not date or month_start.day != 1
            or type(window_start) is not date or window_start > month_start):
        raise ValueError('Explicit source window and month required.')
    if (not isinstance(policy_digest, str) or len(policy_digest) != 64
            or any(c not in '0123456789abcdef' for c in policy_digest)):
        raise ValueError('Explicit policy digest required.')
    acquire_credential_write_lock(db, expected_epoch=expected_epoch)
    db.execute(text('LOCK TABLE departments, employees, employee_service_periods, '
                    'leave_register_entries, leave_holidays, leave_month_close, '
                    'leave_balance_transactions, leave_types IN SHARE MODE'))
    if db.connection().get_isolation_level() != 'READ COMMITTED':
        raise RuntimeError('Fresh READ COMMITTED transaction required.')
    end = date(month_start.year, month_start.month, monthrange(month_start.year, month_start.month)[1])
    ids = sorted(employee_ids)
    def rows(stmt):
        return [dict(row) for row in db.execute(stmt).mappings()]
    with db.no_autoflush:
        department = rows(select(Department.id, Department.name, Department.active).where(Department.id == department_id))
        employees = rows(select(Employee.id, Employee.code, Employee.full_name, Employee.department_id,
                         Employee.hire_date, Employee.birth_date,
                         cast(Employee.prior_insurance_years, String).label('prior_insurance_years'))
                         .where(Employee.id.in_(ids)).order_by(Employee.id))
        if not department or len(employees) != len(ids) or any(e['department_id'] != department_id for e in employees):
            raise ValueError('Resolved employee/department selection no longer matches.')
        periods = rows(select(EmployeeServicePeriod.id, EmployeeServicePeriod.employee_id,
            EmployeeServicePeriod.start_date, EmployeeServicePeriod.end_date)
            .where(EmployeeServicePeriod.employee_id.in_(ids)).order_by(EmployeeServicePeriod.id))
        register = rows(select(LeaveRegisterEntry.id, LeaveRegisterEntry.employee_id,
            LeaveRegisterEntry.work_date, LeaveRegisterEntry.code, LeaveRegisterEntry.revision,
            LeaveRegisterEntry.certificate_attached, LeaveRegisterEntry.leave_record_id)
            .where(LeaveRegisterEntry.employee_id.in_(ids), LeaveRegisterEntry.work_date >= window_start,
                   LeaveRegisterEntry.work_date <= end).order_by(LeaveRegisterEntry.id))
        ledger = rows(select(LeaveBalanceTransaction.id, LeaveBalanceTransaction.employee_id,
            LeaveBalanceTransaction.leave_type_id, LeaveBalanceTransaction.transaction_type,
            cast(LeaveBalanceTransaction.days_delta, String).label('days_delta'),
            cast(LeaveBalanceTransaction.exact_numerator, String).label('exact_numerator'),
            cast(LeaveBalanceTransaction.exact_denominator, String).label('exact_denominator'),
            LeaveBalanceTransaction.leave_record_id, LeaveBalanceTransaction.source_operation_key,
            LeaveBalanceTransaction.source_digest, LeaveBalanceTransaction.earning_year,
            LeaveBalanceTransaction.released_on).where(LeaveBalanceTransaction.employee_id.in_(ids))
            .order_by(LeaveBalanceTransaction.id))
        for row in ledger:
            if row['exact_numerator'] is None and row['exact_denominator'] is None:
                # Preserve legacy source canonical bytes, including PG finite scale.
                del row['exact_numerator']
                del row['exact_denominator']
            else:
                from types import SimpleNamespace
                from decimal import Decimal
                from app.leave_exact_amount import ledger_amount, exact_text
                amount = ledger_amount(SimpleNamespace(days_delta=None if row['days_delta'] is None else Decimal(row['days_delta']),
                    exact_numerator=Decimal(row['exact_numerator']) if row['exact_numerator'] is not None else None,
                    exact_denominator=Decimal(row['exact_denominator']) if row['exact_denominator'] is not None else None))
                row['days_delta'] = exact_text(amount)
        kinds = rows(select(LeaveType.id, LeaveType.code, LeaveType.active, LeaveType.tracks_balance).order_by(LeaveType.id))
        holidays = rows(select(LeaveHoliday.id, LeaveHoliday.holiday_date).where(
            LeaveHoliday.holiday_date >= window_start, LeaveHoliday.holiday_date <= end).order_by(LeaveHoliday.id))
        closures = rows(select(LeaveMonthClose.id, LeaveMonthClose.year, LeaveMonthClose.month,
            LeaveMonthClose.policy_snapshot_json).where(
                (LeaveMonthClose.year * 12 + LeaveMonthClose.month) >= window_start.year * 12 + window_start.month,
                (LeaveMonthClose.year * 12 + LeaveMonthClose.month) <= end.year * 12 + end.month).order_by(LeaveMonthClose.id))
    employed = tuple((i, employed_dates(month_start, end,
        [(p['start_date'], p['end_date']) for p in periods if p['employee_id'] == i])) for i in ids)
    missing = tuple(i for i in ids if not any(p['employee_id'] == i for p in periods))
    source = {'version': 1, 'department': department, 'employees': employees, 'service_periods': periods,
        'register': register, 'ledger': ledger, 'leave_types': kinds, 'holidays': holidays, 'closures': closures,
        'window_start': window_start, 'month_start': month_start, 'policy_digest': policy_digest}
    canonical = json.dumps(source, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                           default=lambda value: value.isoformat() if type(value) is date else (_ for _ in ()).throw(TypeError()))
    return WorkbookSourceFacts(canonical, sha256(canonical.encode()).hexdigest(), employed, missing)
