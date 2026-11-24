"""Actual PostgreSQL read-only selected-source reconciliation, synthetic only."""
from datetime import date,timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select,func

from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_manual_opening_groups import persist_manual_opening_group
from app.leave_opening_source import confirm_opening_source,review_opening_source
from app.leave_policy_preview import LeavePolicy
from app.leave_policy_store import persist_policy_version
from app.models import Employee,EmployeeServicePeriod,LeaveRegisterEntry,LeaveBalanceTransaction,LeaveType,AuditLog
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa


def setup_source(history_context,*,hire=date(2026,2,25),end=date(2026,2,28)):
    db,_actor,auth,fact=history_context
    e=db.get(Employee,fact.employee_id);e.hire_date=hire
    db.add(EmployeeServicePeriod(employee_id=e.id,start_date=hire))
    for n in range((end-hire).days+1):
        day=hire+timedelta(days=n)
        db.add(LeaveRegisterEntry(employee_id=e.id,work_date=day,code='A' if day==end else '',source='synthetic',recorded_by='synthetic'))
    group,_=persist_manual_opening_group(db,employee_id=e.id,balances={'A':Decimal('8.2500000000000001'),'W':Decimal(2),'H':Decimal(0)},effective_start=hire,reason='Synthetic reconciliation',**auth)
    p=persist_policy_version(db,policy=LeavePolicy(False,False,True,False,'calendar','prorated',False,(90,180)),reason='Synthetic explicit',**auth)
    review=review_opening_source(db,group_id=group.id,policy_id=p.id,**auth)
    selected=confirm_opening_source(db,group_id=group.id,policy_id=p.id,expected_previous_id=None,reviewed_source_sha256=review['source_sha256'],reason='Synthetic',human_confirmed=True,**auth)
    db.flush()
    months=[date(end.year,m,1) for m in range(1,end.month+1)]
    return db,e,dict(selection_ids=(selected.id,),department_id=e.department_id,month_start=end.replace(day=1),
        current_insurance_years={(e.id,m):None for m in months},age_evaluation_dates={m:m for m in months},**auth)


def test_newhire_earned_held_until_full_calendar_month_no_ledger_or_audit_write(history_context):
    db,e,args=setup_source(history_context)
    before=[db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,AuditLog,LeaveRegisterEntry)]
    r=reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    annual=next(x for x in r.rows if x.code=='A')
    assert annual.earned_since_opening>0 and annual.released_earned_since_opening==0
    assert annual.held_earned_since_opening==annual.earned_since_opening
    assert annual.used_since_opening==1
    assert annual.released_scenario_balance==Decimal('7.2500000000000001')
    assert not r.establishes_spendable_credit and not r.posting_allowed
    assert 'selected_opening_is_not_released_credit' in annual.issues
    assert [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,AuditLog,LeaveRegisterEntry)]==before


def test_full_march_then_april_releases_february_retroactively_without_adding_ledger(history_context):
    db,e,args=setup_source(history_context,end=date(2026,3,31))
    # Source existing ledger deliberately disagrees with scenario, never add it.
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    assert kind is not None
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,transaction_type='adjustment',
        days_delta=Decimal('-2.12345678901234567890123456789012345678901234567890123456789'),reason='Synthetic unrelated ledger',created_by='synthetic'))
    db.flush()
    held=reconcile_selected_opening(db,release_evaluated_on=date(2026,3,31),**args)
    r=reconcile_selected_opening(db,release_evaluated_on=date(2026,4,1),**args)
    a=next(x for x in r.rows if x.code=='A');old=next(x for x in held.rows if x.code=='A')
    assert a.released_earned_since_opening==a.earned_since_opening and a.held_earned_since_opening==0
    assert old.held_earned_since_opening>0
    assert a.ledger_total_separate==Decimal('-2.12345678901234567890123456789012345678901234567890123456789')
    assert a.earned_scenario_balance==a.released_scenario_balance
    assert 'ledger_overlap_not_reconciled' in a.issues
    assert held.reconciliation_digest!=r.reconciliation_digest


def test_unknown_register_and_stale_identity_refuse_not_presence(history_context):
    db,e,args=setup_source(history_context)
    row=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id))
    row.code=None;db.flush()
    with pytest.raises(ValueError,match='Unknown'): reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    row.code='';db.flush()
    with pytest.raises(ValueError): reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**{**args,'expected_epoch':'stale'})
    with pytest.raises(ValueError): reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**{**args,'session_version':args['session_version']+1})
    with pytest.raises(ValueError): reconcile_selected_opening(db,release_evaluated_on=date(2026,2,1),**args)


def test_interrupted_full_month_does_not_release(history_context):
    db,e,args=setup_source(history_context,end=date(2026,2,28))
    period=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id))
    period.end_date=date(2026,3,15);db.flush()
    r=reconcile_selected_opening(db,release_evaluated_on=date(2026,4,1),**args)
    annual=next(x for x in r.rows if x.code=='A')
    assert annual.released_earned_since_opening==0
    assert 'full_calendar_month_evidence_unresolved' in annual.issues



def test_midmonth_basis_does_not_double_count_prior_usage_and_low_context_is_stable(history_context):
    from decimal import localcontext
    db,e,args=setup_source(history_context,hire=date(2026,5,17),end=date(2026,5,31))
    normal=reconcile_selected_opening(db,release_evaluated_on=date(2026,5,31),**args)
    with localcontext() as context:
        context.prec=4
        low=reconcile_selected_opening(db,release_evaluated_on=date(2026,5,31),**args)
    assert normal==low
    a=next(x for x in normal.rows if x.code=='A')
    assert a.opening==Decimal('8.2500000000000001') and a.used_since_opening==1
    assert a.earned_since_opening<Decimal('1.25')
    assert a.ledger_total_separate==0



def test_release_evidence_change_binds_digest_even_same_earned_projection(history_context):
    db,e,args=setup_source(history_context)
    first=reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    p=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id))
    p.end_date=date(2026,3,15);db.flush()
    changed=reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    assert first.reconciliation_digest!=changed.reconciliation_digest


def test_distinct_opening_basis_after_hire_clips_accrual_and_prior_use_once(history_context):
    from sqlalchemy import delete
    db,e,args=setup_source(history_context,hire=date(2026,5,17),end=date(2026,6,30))
    # Manual basis stays May17, but employee actually started January1.
    e.hire_date=date(2026,1,1)
    service=db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==e.id))
    service.start_date=date(2026,1,1)
    db.add(LeaveRegisterEntry(employee_id=e.id,work_date=date(2026,5,1),code='A',source='synthetic',recorded_by='synthetic'))
    db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,5,20))).code='A'
    db.flush()
    result=reconcile_selected_opening(db,release_evaluated_on=date(2026,7,1),**args)
    a=next(r for r in result.rows if r.code=='A')
    assert result.opening_effective_start==date(2026,5,17)
    assert a.used_since_opening==2  # May20 + June30, not May1 before basis.
    assert Decimal('1.8')<a.earned_since_opening<Decimal('1.9')  # May15/31 + June, not YTD.
    assert a.released_earned_since_opening==a.earned_since_opening
    db.execute(delete(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,6,15)))
    db.flush()
    with pytest.raises(ValueError,match='Unknown'):
        reconcile_selected_opening(db,release_evaluated_on=date(2026,7,1),**args)



def test_small_materialization_bounds_refuse_before_projection(history_context,monkeypatch):
    db,e,args=setup_source(history_context)
    monkeypatch.setattr('app.leave_source_reconciliation.MAX_SERVICE_ROWS',0)
    with pytest.raises(ValueError,match='service evidence'):
        reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    monkeypatch.setattr('app.leave_source_reconciliation.MAX_SERVICE_ROWS',20000)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,transaction_type='adjustment',days_delta=Decimal(1),reason='Synthetic limit',created_by='synthetic'));db.flush()
    monkeypatch.setattr('app.leave_source_reconciliation.MAX_LEDGER_ROWS',0)
    with pytest.raises(ValueError,match='ledger evidence'):
        reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)


def test_actual_linked_legacy_use_classified_without_double_debit_or_netting(history_context):
    from app.models import LeaveRecord
    db,e,args=setup_source(history_context)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    record=LeaveRecord(employee_id=e.id,leave_type_id=kind.id,start_date=date(2026,2,28),end_date=date(2026,2,28),
        calendar_days=1,charged_days=Decimal(1),reason='Synthetic linked use',status='confirmed',created_by='synthetic')
    db.add(record);db.flush()
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,28)))
    day.leave_record_id=record.id
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,leave_record_id=record.id,transaction_type='leave_use',days_delta=Decimal(-1),reason='Synthetic linked use',created_by='synthetic'));db.flush()
    result=reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    assert len(result.overlap_candidates)==1
    candidate=result.overlap_candidates[0]
    assert candidate.state=='explicit_link_candidate_not_reconciled' and not candidate.netting_allowed
    annual=next(r for r in result.rows if r.code=='A')
    assert annual.used_since_opening==1 and annual.ledger_total_separate==-1
    assert annual.released_scenario_balance==Decimal('7.2500000000000001')
    assert not result.posting_allowed


def test_linked_foreign_employee_register_not_silently_omitted(history_context):
    from app.models import LeaveRecord
    db,e,args=setup_source(history_context)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    record=LeaveRecord(employee_id=e.id,leave_type_id=kind.id,start_date=date(2026,2,28),end_date=date(2026,2,28),
        calendar_days=1,charged_days=Decimal(1),reason='Synthetic linked conflict',status='confirmed',created_by='synthetic')
    db.add(record);db.flush()
    foreign=Employee(code='SYN-FOREIGN-OVERLAP',full_name='Synthetic outside',department_id=e.department_id,
        position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
    db.add(foreign);db.flush()
    db.add(LeaveRegisterEntry(employee_id=foreign.id,work_date=date(2026,2,28),code='A',leave_record_id=record.id,source='synthetic',recorded_by='synthetic'))
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,leave_record_id=record.id,transaction_type='leave_use',days_delta=Decimal(-1),reason='Synthetic linked conflict',created_by='synthetic'));db.flush()
    r=reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    c=r.overlap_candidates[0]
    assert c.state=='unresolved_link_conflict'
    assert 'register_record_identity_or_code_mismatch' in c.issues
    assert len(c.register_ids)==1 and not c.netting_allowed



def test_actual_nontracked_annual_type_with_linked_use_not_candidate(history_context):
    from app.models import LeaveRecord
    db,e,args=setup_source(history_context)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'));kind.tracks_balance=False
    record=LeaveRecord(employee_id=e.id,leave_type_id=kind.id,start_date=date(2026,2,28),end_date=date(2026,2,28),calendar_days=1,
        charged_days=Decimal(1),reason='Synthetic nontracked conflict',status='confirmed',created_by='synthetic')
    db.add(record);db.flush()
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,28)));day.leave_record_id=record.id
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,leave_record_id=record.id,transaction_type='leave_use',days_delta=Decimal(-1),reason='Synthetic nontracked conflict',created_by='synthetic'));db.flush()
    result=reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args)
    candidate=result.overlap_candidates[0]
    assert candidate.state=='unresolved_link_conflict' and 'record_type_not_balance_tracked' in candidate.issues
    assert not candidate.netting_allowed



def test_register_share_lock_is_held_before_projection_and_usage_reads(history_context,monkeypatch):
    from sqlalchemy import text
    import app.leave_source_reconciliation as module
    db,e,args=setup_source(history_context)
    original=module._confirmed_department_projection
    def inspected(db,**kwargs):
        for table in ('leave_register_entries','leave_records','leave_balance_transactions','leave_types'):
            assert db.scalar(text("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND relation=to_regclass(:table) AND mode='ShareLock' AND granted"),{'table':table})==1
        return original(db,**kwargs)
    monkeypatch.setattr(module,'_confirmed_department_projection',inspected)
    assert module.reconcile_selected_opening(db,release_evaluated_on=date(2026,2,28),**args).posting_allowed is False
