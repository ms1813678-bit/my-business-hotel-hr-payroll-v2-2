"""Actual PG source-bound monthly day-only handoff."""
from datetime import date
from dataclasses import fields
from decimal import Decimal

import pytest
from sqlalchemy import func,select
from app.leave_payroll_read import get_month_summary,PayrollLeaveMonthSummary
from app.models import LeaveBalanceTransaction,LeaveRegisterEntry,AuditLog,PayrollItem,AdminAccount,LeaveMonthNote
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_source_reconciliation import setup_source


def setup(history_context):
    db,e,args=setup_source(history_context)
    args['release_evaluated_on']=date(2026,2,28)
    call=dict(employee_id=e.id,year=2026,month=2,**args)
    return db,e,call


def counts(db):
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem,LeaveRegisterEntry,AuditLog))


def test_month_counts_not_cumulative_balance_and_no_write(history_context):
    db,e,call=setup(history_context)
    before=counts(db)
    result=get_month_summary(db,**call)
    assert result.employed_days==4 and result.present_days==3
    assert dict(result.codes)=={'U':0,'S':0,'A':1,'W':0,'H':0}
    assert result.unknown_days==0 and not result.conflicting_dates
    assert not result.available_balance_established and not result.posting_allowed and not result.payroll_money_allowed
    assert len(result.balance_evidence)==3
    assert all(r.current_balance is None and r.available_balance is None for r in result.balance_evidence)
    assert counts(db)==before
    assert get_month_summary(db,**call)==result


def test_note_and_register_code_change_bind_evidence(history_context):
    db,e,call=setup(history_context)
    initial=get_month_summary(db,**call)
    row=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id))
    row.notes='Synthetic source-only note';db.flush()
    changed=get_month_summary(db,**call)
    assert changed.source_digest!=initial.source_digest and changed.notes==('Synthetic source-only note',)
    row.code='S';db.flush()
    sick=get_month_summary(db,**call)
    assert dict(sick.codes)['S']==1 and sick.present_days==2 and sick.source_digest!=changed.source_digest


@pytest.mark.parametrize('change',['otheremployee','wrongmonth','invalidmonth','boolyear','unknown','staleepoch','role'])
def test_invalid_or_unresolved_month_refuses(history_context,change):
    db,e,call=setup(history_context)
    if change=='otheremployee':call['employee_id']+=999
    elif change=='wrongmonth':call['month']=3
    elif change=='invalidmonth':call['month']=13
    elif change=='boolyear':call['year']=True
    elif change=='unknown':
        row=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id));row.code=None;db.flush()
    elif change=='staleepoch':call['expected_epoch']='stale'
    elif change=='role':db.get(AdminAccount,call['actor_id']).role='finance';db.flush()
    before=counts(db)
    with pytest.raises(ValueError):get_month_summary(db,**call)
    assert counts(db)==before
