# ruff: noqa: F811
"""Read-only composition for one year-2 annual employee: year-2 net source -> recorded completed-year source (v1)
-> annual settlement review/apply (real cash debit) -> post-cash v2 source. Every link is checked against the previous
one with exact fractions. Uses only published APIs and the owner's recorded annual rule (cash = min(half of the full
entitlement, positive balance), remainder carried). No new rule. py3.10 shim, fixed clock: values are read-only
source amounts, not balances or cash."""
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_annual_settlement_store import apply_annual_settlement, review_annual_settlement_apply
from app.leave_year2_net_source import review_year2_net_source
from app.leave_year2_recorded_source import build_year2_recorded_source
from app.leave_year2_recorded_source_v2 import build_year2_recorded_source_v2
from app.models import LeaveAnnualSettlement, LeaveBalanceTransaction
from tests.test_l8_year2_net_source import FULL
from tests.test_l8_year2_post_cash import settled_2026  # noqa: F401
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_recorded_source import annual_year
from tests.test_leave_cash_out import payroll
import tests.test_l8_year2_post_cash as post_cash


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_net_to_completed_source_to_annual_cash_to_post_cash_v2_compose_exactly(prior_year, monkeypatch):
    from datetime import date
    from app.leave_exact_amount import as_fraction
    monkeypatch.setattr('app.leave_cash_out.datetime', post_cash.Clock)
    db, args = annual_year(prior_year, monkeypatch)
    e, t = args['employee_id'], args['leave_type_id']
    # 1. year-2 net source (read-only) and 2. its recorded completed-year v1 source: same amount, same net digest chain
    net = review_year2_net_source(db, **args)
    n = as_fraction(net['amount'])
    v1 = build_year2_recorded_source(db, **args)
    assert v1['amount'] == n and v1['posting_allowed'] is False
    # the net is own year-tagged rows plus usage attributed to the earlier grant, added back exactly once
    assert n == as_fraction(net['own_rows_amount']) + as_fraction(net['attributed']) and as_fraction(net['attributed']) > 0
    # 3. annual settlement review binds that source and applies the owner's rule exactly
    monkeypatch.setattr('app.leave_carry_expiry.datetime', post_cash.Clock)
    from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry
    old = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.source_year == 2025))
    expiry = dict(grant_kind='annual', grant_id=old.id, employee_id=e, leave_type_id=t, evaluated_on=date(2027, 1, 2),
        reason='Synthetic explicit old annual expiry',
        **{k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')})
    reviewed = review_carry_expiry(db, **expiry)
    apply_carry_expiry(db, operation_id='8' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **expiry)
    _run, item = payroll(db, prior_year[1], start=date(2026, 12, 1), end=date(2026, 12, 31), status='draft')
    selection = dict(source_selection=args, full_annual_entitlement=21, payroll_item_id=item.id,
        available_from=date(2027, 1, 2), available_through=date(2027, 4, 30), reason='Synthetic year2 composition')
    review = review_annual_settlement_apply(db, **selection)
    annual = review['plan']['annual']
    assert annual['source']['version'] == 'year2-recorded-v1'
    assert Fraction(annual['source']['net_amount']) == n and Fraction(annual['remaining']) == n
    cash = Fraction(annual['cash_days'])
    carry = Fraction(annual['carried_days'])
    assert cash == min(Fraction(21, 2), max(n, Fraction(0))) and cash + carry == n   # owner rule, exact
    apply_annual_settlement(db, operation_id='9' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    # 4. the real cash debit exists once, and post-cash v2 = pre-cash net minus exactly that debit
    rows = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.employee_id == e,
        LeaveBalanceTransaction.earning_year == 2026, LeaveBalanceTransaction.source_operation_key.like('leave_cashout:%'))).all()
    assert len(rows) == 1 and as_fraction(rows[0].days_delta) == -cash
    v2 = build_year2_recorded_source_v2(db, **args)
    proof = v2['stored']['source']
    assert [a['kind'] for a in proof['allocations']] == ['cash'] and proof['allocations'][0]['ledger_id'] == rows[0].id
    assert Fraction(proof['base']['carried_amount']) == n and Fraction(proof['net_amount']) == n - cash == carry
    assert v2['posting_allowed'] is False
