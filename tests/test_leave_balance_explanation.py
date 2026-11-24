from datetime import date
from decimal import Decimal
from sqlalchemy import select,func
from app.leave_balance_explanation import explain_selected_balance
from app.models import LeaveBalanceTransaction,AuditLog,LeaveType
from tests.test_leave_source_reconciliation import setup_source
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def test_held_released_usage_ledger_never_double_counted(history_context):
    db,e,args=setup_source(history_context,end=date(2026,3,31))
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,transaction_type='adjustment',days_delta=Decimal('-2.5'),reason='Synthetic debt',created_by='synthetic'));db.flush()
    before=[db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,AuditLog)]
    held=explain_selected_balance(db,release_evaluated_on=date(2026,3,31),**args)
    released=explain_selected_balance(db,release_evaluated_on=date(2026,4,1),**args)
    a=next(r for r in held.rows if r.code=='A');b=next(r for r in released.rows if r.code=='A')
    assert a.held_earned==a.earned and a.released_earned==0
    assert b.held_earned==0 and b.released_earned==b.earned
    assert a.registered_usage==b.registered_usage==1
    assert a.legacy_ledger_separate==b.legacy_ledger_separate==Decimal('-2.5')
    assert b.released_scenario==b.earned_scenario
    assert a.released_scenario==Decimal('7.2500000000000001')
    assert b.current_balance is None and b.available_balance is None
    assert not held.posting_allowed and not b.netting_allowed
    assert [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,AuditLog)]==before


def test_linked_use_candidate_visible_without_netting_or_double_debit(history_context):
    from app.models import LeaveRecord,LeaveRegisterEntry
    db,e,args=setup_source(history_context)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    record=LeaveRecord(employee_id=e.id,leave_type_id=kind.id,start_date=date(2026,2,28),end_date=date(2026,2,28),calendar_days=1,charged_days=Decimal(1),reason='Synthetic linked',status='confirmed',created_by='synthetic')
    db.add(record);db.flush()
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,28)))
    day.leave_record_id=record.id
    db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,leave_record_id=record.id,transaction_type='leave_use',days_delta=Decimal(-1),reason='Synthetic linked',created_by='synthetic'));db.flush()
    result=explain_selected_balance(db,release_evaluated_on=date(2026,2,28),**args)
    a=next(r for r in result.rows if r.code=='A')
    assert a.registered_usage==1 and a.legacy_ledger_separate==-1
    assert len(a.overlap_candidates)==1 and len(result.all_overlap_candidates)==1
    assert a.overlap_candidates[0].state=='explicit_link_candidate_not_reconciled'
    assert a.available_balance is None and a.current_balance is None
    assert a.released_scenario==Decimal('7.2500000000000001')
    assert not a.netting_allowed


def test_cancelled_linked_raw_usage_kept_but_verified_total_unknown(history_context):
    from app.models import LeaveRecord,LeaveRegisterEntry
    db,e,args=setup_source(history_context)
    kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
    record=LeaveRecord(employee_id=e.id,leave_type_id=kind.id,start_date=date(2026,2,28),end_date=date(2026,2,28),calendar_days=1,charged_days=Decimal(1),reason='Synthetic cancelled conflict',status='cancelled',created_by='synthetic')
    db.add(record);db.flush()
    day=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,2,28)))
    day.leave_record_id=record.id
    for typ,amount in [('leave_use',Decimal(-1)),('leave_reversal',Decimal(1))]:
        db.add(LeaveBalanceTransaction(employee_id=e.id,leave_type_id=kind.id,leave_record_id=record.id,transaction_type=typ,days_delta=amount,reason='Synthetic cancelled',created_by='synthetic'))
    db.flush()
    result=explain_selected_balance(db,release_evaluated_on=date(2026,2,28),**args)
    a=next(r for r in result.rows if r.code=='A')
    assert a.registered_usage==1 and a.nonconflicting_raw_usage is None
    assert a.legacy_ledger_separate==0 and a.current_balance is None
    assert a.overlap_candidates[0].state=='cancelled_record_requires_review'
