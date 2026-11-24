"""Pure annual tier preview, no date/insurance inference or balance posting."""
from dataclasses import dataclass
from decimal import Decimal

from app.leave_policy_preview import LeavePolicy


@dataclass(frozen=True)
class AnnualTierPreview:
    tier: int
    unresolved_evidence: tuple[str, ...]


def annual_tier_preview(*, service_year: int, age_years: int | None,
                        insured_years: Decimal | None,
                        policy: LeavePolicy) -> AnnualTierPreview:
    """Caller supplies anniversary-month service-year classification explicitly.

    Unknown enabled evidence is visible, not silently assumed below threshold.
    A30-day qualifying fact is enough even when the alternative is unknown.
    Returned provisional tier must not be posted while unresolved evidence
    remains. No birth date/insurance value is derived from national ID/tenure.
    """
    if isinstance(service_year, bool) or not isinstance(service_year, int) or service_year < 1:
        raise ValueError('Explicit positive service year required.')
    if age_years is not None and (isinstance(age_years, bool) or not isinstance(age_years, int) or age_years < 0):
        raise ValueError('Age must be explicit nonnegative integer or unknown.')
    if insured_years is not None and (not isinstance(insured_years, Decimal)
            or not insured_years.is_finite() or insured_years < 0):
        raise ValueError('Insurance years must be explicit nonnegative Decimal or unknown.')
    if ((policy.tier30_by_age_50 and age_years is not None and age_years >= 50)
            or (policy.tier30_by_insurance_years and insured_years is not None and insured_years >= 10)):
        return AnnualTierPreview(30, ())
    unresolved = []
    if policy.tier30_by_age_50 and age_years is None:
        unresolved.append('age_years')
    if policy.tier30_by_insurance_years and insured_years is None:
        unresolved.append('insured_years')
    return AnnualTierPreview(15 if service_year == 1 else 21, tuple(unresolved))
