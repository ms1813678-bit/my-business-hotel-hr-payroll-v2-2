"""Cumulative earned-only planning projection, no usable-credit/posting authority.

Consumes the locked DB fact snapshot, explicit tier schedule/policy and exact
resolved carry/opening evidence. Never substitutes all-time ledger SUM, an
unknown register day, or a current-active flag for these sources. Caller must
keep source transaction alive and bind selected inputs into its source digest.
"""
import json
from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, localcontext
from hashlib import sha256

from app.department_workbook import BalanceSnapshot, DepartmentWorkbookEmployee
from app.department_workbook_source import WorkbookSourceFacts
from app.leave_day_calculations import (
    annual_month_accrued,
    employed_dates,
    holiday_credit,
    weekly_accrued,
)
from app.leave_policy_preview import LeavePolicy, annual_deductible_count


@dataclass(frozen=True)
class EarnedWorkbookProjection:
    employees: tuple[DepartmentWorkbookEmployee, ...]
    source_digest: str
    earned_only: bool = True
    establishes_spendable_credit: bool = False
    unresolved_target_days: tuple[tuple[int, int], ...] = ()
    opening_effective_start: date | None = None
    integrity_sha256: str = ''
    policy_independent_source_sha256: str = ''


def project_earned_workbook(*, facts: WorkbookSourceFacts, policy: LeavePolicy,
                            opening: dict[tuple[int, str], Decimal],
                            annual_tiers: dict[tuple[int, date], int],
                            planning_target_month: bool = False,
                            opening_effective_start: date | None = None) -> EarnedWorkbookProjection:
    """Prior-month days must be resolved. Output is NOT released annual credit.

    Explicit target-month planning may project unknown service days without
    assigning attendance codes. Known U/S still reduce projected accrual.
    Unknown counts remain explicit and final balances require real evidence.

    Year is explicitly calendar, confirmed owner basis. Tier schedule comes
    from separately resolved anniversary/age/insurance evidence, not a guessed
    constant. Opening covers carry once by category, including negative debt.
    Availability windows/settlements are NOT solved here; caller must select
    valid opening evidence. No missing opening is inferred zero.
    """
    if not isinstance(facts, WorkbookSourceFacts) or not isinstance(policy, LeavePolicy):
        raise TypeError('Explicit source facts and resolved policy required.')
    source = json.loads(facts.canonical_json)
    target = date.fromisoformat(source['month_start'])
    first = date.fromisoformat(source['window_start'])
    if policy.annual_year_basis != 'calendar' or first != date(target.year, 1, 1):
        raise ValueError('Calendar year source window required.')
    if facts.missing_service_employee_ids:
        raise ValueError('Resolve missing service evidence before projection.')
    basis_start = opening_effective_start or first
    if type(basis_start) is not date or not first <= basis_start <= date(target.year, target.month, monthrange(target.year, target.month)[1]):
        raise ValueError('Opening basis must be inside projection year through selected month.')
    ids = {e['id'] for e in source['employees']}
    expected = {(i, c) for i in ids for c in ('A', 'H', 'W')}
    if set(opening) != expected or any(not isinstance(v, Decimal) or not v.is_finite() for v in opening.values()):
        raise ValueError('Exact complete resolved opening evidence required.')
    months = [date(target.year, m, 1) for m in range(1, target.month + 1)]
    if set(annual_tiers) != {(i, month) for i in ids for month in months}:
        raise ValueError('Complete explicit annual tier schedule required.')
    if type(planning_target_month) is not bool:
        raise TypeError('Explicit planning mode required.')
    output = []
    unresolved = []
    for employee in source['employees']:
        i = employee['id']
        periods = [(date.fromisoformat(p['start_date']), date.fromisoformat(p['end_date']) if p['end_date'] else None)
                   for p in source['service_periods'] if p['employee_id'] == i]
        entries = {date.fromisoformat(r['work_date']): r['code'] for r in source['register'] if r['employee_id'] == i}
        earned = dict.fromkeys(('A', 'H', 'W'), Decimal(0))
        used_before = dict.fromkeys(('A', 'H', 'W'), Decimal(0))
        target_service = frozenset()
        with localcontext() as context:
            context.prec = 50
            for month in months:
                end = date(month.year, month.month, monthrange(month.year, month.month)[1])
                service = employed_dates(max(month, basis_start), end, periods) if end >= basis_start else frozenset()
                if month == target:
                    target_service = employed_dates(month, end, periods)
                unknown = {day for day in service if day not in entries or entries[day] is None}
                if any(entries.get(day) not in (None, '', 'U', 'S', 'W', 'H', 'A') for day in service):
                    raise ValueError('Conflicting employed day prevents earned projection.')
                if unknown and not (month == target and planning_target_month):
                    raise ValueError('Unknown employed day prevents earned projection.')
                if month == target:
                    unresolved.append((i, len(unknown)))
                if any(max(month, basis_start) <= day <= end and day not in service and code not in (None, '—') for day, code in entries.items()):
                    raise ValueError('Day evidence outside service conflicts with source.')
                counts = {code: sum(entries.get(d) == code for d in service) for code in ('U', 'S', 'W', 'H', 'A')}
                earned['W'] += weekly_accrued(len(service), counts['U'], counts['S'])
                earned['A'] += annual_month_accrued(annual_tiers[i, month], end.day, len(service),
                    annual_deductible_count(counts['U'], counts['S'], policy))
                holidays = {date.fromisoformat(h['holiday_date']) for h in source['holidays']
                            if month <= date.fromisoformat(h['holiday_date']) <= end}
                earned['H'] += holiday_credit(holidays, service, {d for d in service if entries.get(d) == 'S'},
                    credit_during_sick=policy.holiday_credit_during_sick)
                if month < target:
                    for code in used_before:
                        used_before[code] += Decimal(counts[code])
        output.append(DepartmentWorkbookEmployee(i, employee['code'], employee['full_name'],
            max(first, min(start for start, _end in periods)), target_service,
            *(BalanceSnapshot(opening[i, code], earned[code], used_before[code]) for code in ('A', 'H', 'W')),
            balance_usage_from=max(target, basis_start)))
    binding = json.dumps({'facts': facts.digest, 'policy': asdict(policy),
        'planning_target_month': planning_target_month, 'opening_effective_start': basis_start.isoformat(),
        'opening': [[i, c, format(opening[i, c], 'f')] for i, c in sorted(opening)],
        'tiers': [[i, d.isoformat(), annual_tiers[i, d]] for i, d in sorted(annual_tiers)]},
        sort_keys=True, separators=(',', ':'))
    result = EarnedWorkbookProjection(tuple(output), sha256(binding.encode()).hexdigest(),
                                    unresolved_target_days=tuple(unresolved), opening_effective_start=basis_start)
    from dataclasses import replace
    return replace(result, integrity_sha256=projection_integrity(result))


def projection_integrity(projection):
    payload = asdict(projection)
    payload.pop('integrity_sha256', None)
    def encode(value):
        if isinstance(value, Decimal): return format(value, 'f')
        if type(value) is date: return value.isoformat()
        if isinstance(value, frozenset): return sorted(d.isoformat() for d in value)
        raise TypeError('Unexpected projection field.')
    return sha256(json.dumps(payload, sort_keys=True, default=encode, separators=(',', ':')).encode()).hexdigest()
