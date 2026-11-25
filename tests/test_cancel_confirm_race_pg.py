"""Two real PostgreSQL sessions: cancel must not overwrite a concurrently confirmed draft without reversing its use."""
import threading
import time
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.db import SessionLocal, engine
from app.leave_workflow import confirm_leave
from app.main import cancel_leave_record
from app.models import (Department, Employee, EmployeeCodeCounter, LeaveBalanceTransaction, LeaveRecord, LeaveType)

pytestmark = [
    pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='Needs PostgreSQL row locks.'),
    # Creates and deletes its own synthetic rows: only run against a scratch DB named like *test* / synthetic_*.
    pytest.mark.skipif(
        'test' not in str(engine.url.database or '').lower() and 'synthetic' not in str(engine.url.database or '').lower(),
        reason='Refusing to create fixtures outside a scratch test database.'),
]


def test_cancel_waits_for_concurrent_confirm_and_reverses_it():
    tag = uuid4().hex[:8]
    with SessionLocal.begin() as db:
        dept = Department(name='Race ' + tag, active=True)
        kind = LeaveType(code='race' + tag, name_ar='x', name_en='x', is_paid=True, tracks_balance=True, active=True)
        db.add_all([dept, kind]); db.flush()
        emp = Employee(code='RC-' + tag, full_name='Race Synthetic', department_id=dept.id, position='t',
                       hire_date=date.today() - timedelta(days=400), status='active',
                       basic_salary=Decimal('5000.00'), daily_hours=Decimal('8'))
        db.add(emp); db.flush()
        rec = LeaveRecord(employee_id=emp.id, leave_type_id=kind.id, start_date=date.today(), end_date=date.today(),
                          calendar_days=1, charged_days=Decimal('1'), reason='race', status='draft', created_by='t')
        db.add(rec); db.flush()
        ids = (dept.id, kind.id, emp.id, rec.id)
    dept_id, kind_id, emp_id, rec_id = ids
    out = {}
    a = SessionLocal(); a.begin()
    try:
        record = a.scalar(select(LeaveRecord).where(LeaveRecord.id == rec_id).with_for_update())
        a.scalar(select(Employee).where(Employee.id == emp_id).with_for_update())
        confirm_leave(a, record, 'confirmer')
        a.flush()                                   # use posted, confirmation NOT yet committed

        def cancel():
            out['resp'] = cancel_leave_record(rec_id, 'race cancel', SimpleNamespace(username='canceller'))
        t = threading.Thread(target=cancel); t.start()
        time.sleep(1.5)
        a.commit()
        t.join(20)
        assert not t.is_alive()
    finally:
        a.close()
    try:
        with SessionLocal() as db:
            rec = db.get(LeaveRecord, rec_id)
            net = db.scalar(select(func.coalesce(func.sum(LeaveBalanceTransaction.days_delta), 0))
                            .where(LeaveBalanceTransaction.leave_record_id == rec_id))
            kinds = sorted(db.scalars(select(LeaveBalanceTransaction.transaction_type)
                                      .where(LeaveBalanceTransaction.leave_record_id == rec_id)).all())
        assert rec.status == 'cancelled', (rec.status, out)
        assert kinds == ['leave_reversal', 'leave_use'], kinds   # cancelled draft-turned-confirmed was reversed
        assert Decimal(net) == 0
    finally:
        with SessionLocal.begin() as db:
            for model, col, val in ((LeaveBalanceTransaction, LeaveBalanceTransaction.leave_record_id, rec_id),
                                    (LeaveRecord, LeaveRecord.id, rec_id),
                                    (Employee, Employee.id, emp_id),
                                    (LeaveType, LeaveType.id, kind_id),
                                    (Department, Department.id, dept_id)):
                db.execute(delete(model).where(col == val))
