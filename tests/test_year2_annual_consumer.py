# ruff: noqa: F811
"""Annual-only year2 consumer candidate, real prior grants/months, fixed 2027 clock."""
from datetime import date
from fractions import Fraction
import json
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from app.models import LeaveAnnualSettlement, LeaveBalanceTransaction
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement, _verify_plan_rule
from app.leave_annual_settlement_source import review_annual_settlement
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_recorded_source import annual_year
from tests.test_l8_year2_net_source import FULL, Clock
from tests.test_leave_cash_out import payroll, counts


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year2_annual_settlement_exact_net_replay_after_cash_growth_and_refusals(prior_year, monkeypatch):
    monkeypatch.setattr('app.leave_cash_out.datetime', Clock)
    db, args = annual_year(prior_year, monkeypatch)
    monkeypatch.setattr('app.leave_carry_expiry.datetime', Clock)
    from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry
    old = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.source_year==2025))
    expiry = dict(grant_kind='annual', grant_id=old.id, employee_id=args['employee_id'],
        leave_type_id=args['leave_type_id'], evaluated_on=date(2027,1,2), reason='Synthetic explicit old annual expiry',
        **{k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')})
    reviewed_expiry=review_carry_expiry(db,**expiry)
    apply_carry_expiry(db,operation_id='8'*64,reviewed_digest=reviewed_expiry['reviewed_digest'],human_confirmed=True,**expiry)
    _run, item = payroll(db, prior_year[1], start=date(2026,12,1), end=date(2026,12,31), status='draft')
    selection=dict(source_selection=args, full_annual_entitlement=21, payroll_item_id=item.id,
        available_from=date(2027,1,2), available_through=date(2027,4,30), reason='Synthetic year2 annual settlement')
    review=review_annual_settlement_apply(db, **selection)
    annual=review['plan']['annual']
    assert annual['source']['version']=='year2-recorded-v1'
    assert Fraction(annual['remaining']) == Fraction(annual['source']['net_amount'])
    assert Fraction(annual['cash_days']) == Fraction(21,2)
    confirm=dict(operation_id='9'*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**selection)
    before=counts(db)
    with pytest.raises(ValueError):apply_annual_settlement(db,**{**confirm,'reviewed_digest':'0'*64})
    assert counts(db)==before
    first=apply_annual_settlement(db,**confirm)
    assert first['carry_duplicate_credit']=='0/1' and not first['cash_payment_committed']
    row=db.get(LeaveAnnualSettlement,'9'*64)
    _verify_plan_rule(db, json.loads(row.result_json)['plan']['annual'], row)
    after=counts(db)
    assert apply_annual_settlement(db,**confirm)==first and counts(db)==after
    # Closed-year source now includes its own cash debit, so fresh derivation
    # is deliberately distinct from receipt-first replay above.
    with pytest.raises(ValueError, match='provenance'):
        review_annual_settlement(db,source_selection=args,full_annual_entitlement=21,other_half_action='carry')
    # New backdated same-year rows change a fresh source, not the named pre-cash
    # recorded proof. Preserve receipt-first retry while still checking named rows.
    later=LeaveBalanceTransaction(employee_id=args['employee_id'],leave_type_id=args['leave_type_id'],
        transaction_type='adjustment',days_delta=1,earning_year=2026,released_on=date(2026,12,31),
        source_operation_key='synthetic_backdated_annual_growth',source_digest='a'*64,reason='Synthetic later backdated provenance',created_by='synthetic')
    db.add(later);db.flush()
    after=counts(db)
    assert apply_annual_settlement(db,**confirm)==first and counts(db)==after
    with pytest.raises(ValueError,match='unrelated ledger'):
        review_annual_settlement(db,source_selection=args,full_annual_entitlement=21,other_half_action='carry')
    named_id=json.loads(row.result_json)['plan']['annual']['source']['own']['source']['selected_ids'][0]
    point=db.begin_nested()
    named=db.get(LeaveBalanceTransaction,named_id)
    named.days_delta+=1
    with pytest.raises(DBAPIError,match='source-bound leave history is immutable'):
        db.flush()
    point.rollback()
    assert apply_annual_settlement(db,**confirm)==first and counts(db)==after
    for identity in ({'session_version':args['session_version']+1},{'expected_epoch':'stale'}):
        bad={**confirm,'source_selection':{**args,**identity}}
        with pytest.raises(ValueError):apply_annual_settlement(db,**bad)
        assert counts(db)==after
