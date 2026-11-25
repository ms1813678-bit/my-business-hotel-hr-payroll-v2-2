"""Defense-in-depth: the reader refuses a positive carry receipt that claims the annual type.

The allocation writer already refuses positive annual, so a valid receipt of that shape cannot exist.
This test forges one inside a scratch database (named *test*) and stubs the two evidence verifiers
that would stop the forgery earlier, so it exercises only the reader's own final annual guard.
"""
from datetime import date

import pytest
from sqlalchemy import select, text

from app import leave_carry_availability as reader
from app.db import engine
from app.models import LeaveCarryAllocation
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_carry_availability import OPERATION, recorded
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa

pytestmark = pytest.mark.skipif(not (engine.url.database or '').startswith(('test_','synthetic_')),
                                reason='forges a receipt: explicit disposable test_/synthetic_ database only')


def test_positive_annual_receipt_is_refused_by_the_reader(prior_year, monkeypatch):
    from app.db import engine
    if not (engine.url.database or '').startswith(('test_','synthetic_')):pytest.skip('Explicit disposable database required.')
    db, employee, kinds, identity = recorded(prior_year)       # real positive weekly-rest receipt
    weekly_id, annual_id = kinds['W'], kinds['A']
    db.execute(text('ALTER TABLE leave_carry_allocations DISABLE TRIGGER USER'))
    db.execute(text('UPDATE leave_carry_allocations SET leave_type_id=:a WHERE id=:i'),
               dict(a=annual_id, i=OPERATION))
    db.execute(text('ALTER TABLE leave_carry_allocations ENABLE TRIGGER USER'))
    db.expire_all()
    real_verify = reader.verify_allocation_evidence
    monkeypatch.setattr(reader, 'verify_allocation_evidence',
                        lambda **kw: real_verify(**{**kw, 'row_leave_type_id': weekly_id}))
    monkeypatch.setattr(reader, '_verify_recorded_source',
                        lambda db, **kw: {'leave_type_code': 'annual'})
    assert db.scalar(select(LeaveCarryAllocation.leave_type_id).where(LeaveCarryAllocation.id == OPERATION)) == annual_id
    with pytest.raises(ValueError, match='Positive annual uses the separate'):
        reader.read_carry_availability(db, allocation_id=OPERATION, employee_id=employee.id,
                                       leave_type_id=annual_id, work_date=date(2026, 2, 1), **identity)
