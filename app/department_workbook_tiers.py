"""Resolve month tier evidence, never infer insurance from service or identity.

Per-month age evaluation dates and current insured years are explicit inputs,
not selected by this module. Birth/prior insurance come from locked sourcefacts.
Returns unresolved evidence rather than silently accepting provisional15/21.
"""
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.department_workbook_source import WorkbookSourceFacts
from app.leave_policy_preview import LeavePolicy, insurance_years
from app.leave_tier_preview import annual_tier_preview


@dataclass(frozen=True)
class WorkbookTierEvidence:
    tiers: dict[tuple[int, date], int]
    unresolved: tuple[tuple[int, date, str], ...]


def resolve_workbook_tiers(*, facts: WorkbookSourceFacts, policy: LeavePolicy,
                           age_evaluation_dates: dict[date, date],
                           current_insurance_years: dict[tuple[int, date], Decimal | None]) -> WorkbookTierEvidence:
    source = json.loads(facts.canonical_json)
    target = date.fromisoformat(source['month_start'])
    months = tuple(date(target.year, m, 1) for m in range(1, target.month + 1))
    if set(age_evaluation_dates) != set(months):
        raise ValueError('Explicit complete age evaluation schedule required.')
    ids = {row['id'] for row in source['employees']}
    if set(current_insurance_years) != {(i, m) for i in ids for m in months}:
        raise ValueError('Explicit current insurance evidence coverage required.')
    tiers = {}; unresolved = []
    for employee in source['employees']:
        i = employee['id']
        hired = date.fromisoformat(employee['hire_date'])
        born = date.fromisoformat(employee['birth_date']) if employee['birth_date'] else None
        prior = Decimal(employee['prior_insurance_years']) if employee['prior_insurance_years'] is not None else None
        for month in months:
            evaluated = age_evaluation_dates[month]
            if type(evaluated) is not date or (evaluated.year, evaluated.month) != (month.year, month.month):
                raise ValueError('Age evaluation date must be explicit and within its month.')
            if born is not None and born > evaluated:
                raise ValueError('Birth date is after age evaluation date.')
            age = None if born is None else evaluated.year - born.year - ((evaluated.month, evaluated.day) < (born.month, born.day))
            # Tier change is in anniversary MONTH, not anniversary day.
            service_year = max(1, month.year - hired.year + 1 - (month.month < hired.month))
            result = annual_tier_preview(service_year=service_year, age_years=age,
                insured_years=insurance_years(prior, current_insurance_years[i, month]), policy=policy)
            if result.unresolved_evidence:
                unresolved.extend((i, month, key) for key in result.unresolved_evidence)
            else:
                tiers[i, month] = result.tier
    return WorkbookTierEvidence(tiers, tuple(unresolved))
