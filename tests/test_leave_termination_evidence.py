from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select
from app.leave_termination_evidence import read_termination_evidence
from app.models import EmployeeServicePeriod,LeaveRegisterEntry
from tests.test_leave_source_reconciliation import setup_source
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa


def setup(history_context):
    db,e,args=setup_source(history_context,hire=date(2026,2,25),end=date(2026,3,15))
    p=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id));p.end_date=date(2026,3,15)
    for n in (1,2,3):db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,3,n))).code='A'
    db.flush()
    return db,e,dict(selection_id=args['selection_ids'][0],department_id=args['department_id'],end_work_date=date(2026,3,15),
        current_insurance_years=args['current_insurance_years'],age_evaluation_dates=args['age_evaluation_dates'],
        release_evaluated_on=date(2026,3,31),**{k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')})


def test_end_work_clips_accrual_and_reports_annual_days_no_deduction(history_context):
    db,e,args=setup(history_context)
    r=read_termination_evidence(db,**args)
    assert r.annual_taken_since_hire==4 and r.unknown_service_days_since_hire==0
    assert r.under_six_months is None and 'six_month_boundary_not_selected' in r.issues
    annual=next(x for x in r.reconciliation.rows if x.code=='A')
    assert annual.earned_since_opening<Decimal('1.25')
    assert annual.released_earned_since_opening==0
    assert not r.posting_allowed and not r.automatic_deduction_allowed and not r.settlement_allowed


def test_open_or_wrong_final_period_refuses_termination(history_context):
    db,e,args=setup(history_context)
    with pytest.raises(ValueError,match='final recorded'):read_termination_evidence(db,**{**args,'end_work_date':date(2026,3,16)})
    p=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id));p.end_date=None;db.flush()
    with pytest.raises(ValueError,match='final recorded'):read_termination_evidence(db,**args)


def test_missing_prior_history_never_becomes_zero_annual_taken(history_context):
    db,e,args=setup(history_context)
    # Current reconciled basisFeb25 needs this day too, so strict reconciliation refuses.
    from sqlalchemy import delete
    db.execute(delete(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,3,5)));db.flush()
    with pytest.raises(ValueError,match='Unknown'):read_termination_evidence(db,**args)



def test_missing_day_before_opening_basis_returns_unknown_not_zero(history_context):
    from app.models import Employee
    db,e,args=setup(history_context)
    # OpeningFeb25, actualhireFeb20. No attendance source forFeb20-24.
    e.hire_date=date(2026,2,20)
    p=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id));p.start_date=date(2026,2,20)
    db.flush()
    r=read_termination_evidence(db,**args)
    assert r.unknown_service_days_since_hire==5 and r.annual_taken_since_hire is None
    assert 'annual_use_history_incomplete_not_zero' in r.issues


def test_hire_service_mismatch_visible_and_post_end_conflict_visible(history_context):
    db,e,args=setup(history_context)
    e.hire_date=date(2026,2,20);db.flush()
    before=read_termination_evidence(db,**args)
    assert 'original_hire_and_first_service_start_disagree' in before.issues
    assert before.annual_taken_since_hire is None
    # Outside targetmonth, projection does not itself inspectthislaterrow.
    db.add(LeaveRegisterEntry(employee_id=e.id,work_date=date(2026,4,1),code='A',source='synthetic',recorded_by='synthetic'));db.flush()
    after=read_termination_evidence(db,**args)
    assert 'register_evidence_after_final_service_end_requires_review' in after.issues
    assert before.evidence_sha256!=after.evidence_sha256
