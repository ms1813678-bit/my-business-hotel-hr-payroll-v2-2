# ruff: noqa: F811
"""Year2 annual-grant expiry shape regression. 2027 monthly setup has an explicit
TEST-ONLY transition admission scaffold because the next-year transition consumer
is not admitted yet. Actual monthly receipts, grant proof, expiry and ledger are real.
This is not end-to-end year3 admission qualification.
"""
from calendar import monthrange
from datetime import date, datetime
from fractions import Fraction
import pytest
from sqlalchemy import select, func
from app.models import CAIRO_TZ, LeaveRegisterEntry, LeaveAnnualSettlement, LeaveBalanceTransaction
from app.leave_month_earning import review_month_earning, apply_month_earning
from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import FULL
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement
from tests.test_l8_year2_recorded_source import annual_year
from tests.test_l8_year2_net_source import Clock
from tests.test_leave_cash_out import payroll

def settled_2026(prior_year, monkeypatch):
    monkeypatch.setattr('app.leave_cash_out.datetime', Clock)
    db, args = annual_year(prior_year, monkeypatch)
    monkeypatch.setattr('app.leave_carry_expiry.datetime', Clock)
    from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry
    old = db.scalar(select(LeaveAnnualSettlement).where(LeaveAnnualSettlement.source_year == 2025))
    expiry = dict(grant_kind='annual', grant_id=old.id, employee_id=args['employee_id'],
        leave_type_id=args['leave_type_id'], evaluated_on=date(2027, 1, 2), reason='Synthetic explicit old annual expiry',
        **{k: args[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')})
    reviewed = review_carry_expiry(db, **expiry)
    apply_carry_expiry(db, operation_id='8' * 64, reviewed_digest=reviewed['reviewed_digest'], human_confirmed=True, **expiry)
    _run, item = payroll(db, prior_year[1], start=date(2026, 12, 1), end=date(2026, 12, 31), status='draft')
    selection = dict(source_selection=args, full_annual_entitlement=21, payroll_item_id=item.id,
        available_from=date(2027, 1, 2), available_through=date(2027, 4, 30), reason='Synthetic year2 annual settlement')
    review = review_annual_settlement_apply(db, **selection)
    apply_annual_settlement(db, operation_id='9' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **selection)
    return db, args


class Clock2028(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2028,1,15,9,0,tzinfo=tz or CAIRO_TZ)

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_year2_annual_v1_grant_expiry_after_complete_window(prior_year,monkeypatch):
    db,args=settled_2026(prior_year,monkeypatch)
    for mod in ('app.leave_month_earning','app.leave_carry_source','app.leave_carry_expiry'):
        monkeypatch.setattr(mod+'.datetime',Clock2028)
    # Deliberate isolated setup scaffold, not a production checkpoint or permission.
    import app.leave_year_transition as transition
    original=transition.verify_transition_for_month
    monkeypatch.setattr(transition,'verify_transition_for_month',lambda db_,**kw:
        [] if kw['month_start'].year==2027 else original(db_,**kw))
    employee=args['employee_id']
    for month in range(1,5):
        for day in range(1,monthrange(2027,month)[1]+1):
            db.add(LeaveRegisterEntry(employee_id=employee,work_date=date(2027,month,day),code='',source='synthetic',recorded_by='synthetic'))
        db.flush()
        months=[date(2027,m,1) for m in range(1,month+1)]
        call=dict(selection_id=args['selection_id'],month_start=months[-1],evaluated_on=date(2027,5,1),
            current_insurance_years={(employee,m):None for m in months},age_evaluation_dates={m:m for m in months},
            **{k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')})
        reviewed=review_month_earning(db,**call)
        apply_month_earning(db,operation_id=format(700+month,'064x'),reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,reason='Synthetic actual grant-window month',**call)
    grant=db.get(LeaveAnnualSettlement,'9'*64)
    expiry=dict(grant_kind='annual',grant_id=grant.id,employee_id=employee,leave_type_id=args['leave_type_id'],
        evaluated_on=date(2028,1,2),reason='Synthetic explicit year2 annual expiry',
        **{k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')})
    # A new unrelated tagged row is NOT silently added to the grant's proven
    # history. It refuses expiry without writes, then rollback restores the proof.
    point=db.begin_nested()
    db.add(LeaveBalanceTransaction(employee_id=employee,leave_type_id=args['leave_type_id'],
        transaction_type='adjustment',days_delta=1,earning_year=2027,released_on=date(2027,4,30),
        source_operation_key='synthetic-expiry-unknown-growth',source_digest='a'*64,
        reason='Synthetic unknown provenance',created_by='synthetic'))
    db.flush()
    blocked=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    with pytest.raises(ValueError,match='Unallocated later ledger'):
        review_carry_expiry(db,**expiry)
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==blocked
    point.rollback()
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    reviewed=review_carry_expiry(db,**expiry)
    assert Fraction(reviewed['plan']['excluded'])>0
    assert reviewed['plan']['consumed']=='0/1'
    assert len(reviewed['plan']['verified_window_months'])==4
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
    confirm=dict(operation_id='7'*64,reviewed_digest=reviewed['reviewed_digest'],human_confirmed=True,**expiry)
    first=apply_carry_expiry(db,**confirm)
    assert apply_carry_expiry(db,**confirm)==first
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before+1
