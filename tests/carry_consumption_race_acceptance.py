"""Run only via fresh migrated scratch wrapper. Committed concurrency proof."""
from datetime import date
import threading
from sqlalchemy import select,func
from sqlalchemy.exc import OperationalError
from fastapi import HTTPException
import pytest
from app.db import SessionLocal
from app.models import LeaveCarryConsumption,LeaveBalanceTransaction
from app.leave_carry_consumption import review_carry_consumption,record_carry_consumption
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_carry_consumption import ready,FULL

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_two_committed_sessions_cannot_attribute_same_day_twice(prior_year):
    db,call=ready(prior_year)
    before=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction));db.commit()
    with SessionLocal() as first:review=review_carry_consumption(first,**call)
    barrier=threading.Barrier(2);out=[];mutex=threading.Lock()
    def go(key):
        try:
            with SessionLocal() as session:
                barrier.wait(timeout=10)
                record_carry_consumption(session,operation_id=key*64,reviewed_digest=review['reviewed_digest'],human_confirmed=True,**call)
                session.commit()
                status='ok'
        except (ValueError,OperationalError,HTTPException):status='refused'
        with mutex:out.append(status)
    threads=[threading.Thread(target=go,args=(key,)) for key in ('a','c')]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(out)==['ok','refused']
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(LeaveCarryConsumption))==1
        assert session.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==before
        with pytest.raises(ValueError):review_carry_consumption(session,**call)
