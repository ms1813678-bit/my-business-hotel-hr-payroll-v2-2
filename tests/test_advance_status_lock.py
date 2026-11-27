"""A stale in-session copy of an advance must not decide approve, reject or paid (AI path loads it without a lock)."""
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.advance_workflow import approve_advance, mark_advance_paid, reject_advance
from app.db import SessionLocal
from app.models import AdvanceInstallment, AdvanceRequest, Department, Employee


def _make():
    code = 'ADV-' + uuid4().hex[:8]
    with SessionLocal.begin() as db:
        dept = Department(name='Advance lock ' + code, active=True)
        db.add(dept)
        db.flush()
        employee = Employee(code=code, full_name='Synthetic ' + code, department_id=dept.id, position='P',
                            hire_date=date(2026, 1, 1), status='active',
                            basic_salary=Decimal('10000'), daily_hours=Decimal('8'))
        db.add(employee)
        db.flush()
        request = AdvanceRequest(employee_id=employee.id, request_date=date(2026, 11, 16),
                                 payroll_month=date(2026, 11, 1), amount=Decimal('100'),
                                 advance_type='regular', salary_basis=Decimal('10000'),
                                 reason='synthetic', status='pending', created_by='synthetic')
        db.add(request)
        db.flush()
        return request.id


def _installments(request_id):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(AdvanceInstallment)
                         .where(AdvanceInstallment.advance_request_id == request_id))


def test_stale_pending_copy_cannot_approve_after_reject():
    request_id = _make()
    with SessionLocal() as stale:
        stale_copy = stale.get(AdvanceRequest, request_id)
        assert stale_copy.status == 'pending'
        with SessionLocal.begin() as other:
            reject_advance(other, other.get(AdvanceRequest, request_id), 'synthetic', 'no')
        with pytest.raises(ValueError, match='pending'):
            approve_advance(stale, stale_copy, 'synthetic')
    assert _installments(request_id) == 0


def test_stale_pending_copy_cannot_reject_after_approve():
    request_id = _make()
    with SessionLocal() as stale:
        stale_copy = stale.get(AdvanceRequest, request_id)
        with SessionLocal.begin() as other:
            approve_advance(other, other.get(AdvanceRequest, request_id), 'synthetic')
        with pytest.raises(ValueError, match='pending'):
            reject_advance(stale, stale_copy, 'synthetic', 'no')
    with SessionLocal() as db:
        assert db.get(AdvanceRequest, request_id).status == 'approved'


def test_stale_approved_copy_cannot_mark_paid_after_it_was_paid():
    request_id = _make()
    with SessionLocal.begin() as db:
        approve_advance(db, db.get(AdvanceRequest, request_id), 'synthetic')
    with SessionLocal() as stale:
        stale_copy = stale.get(AdvanceRequest, request_id)
        with SessionLocal.begin() as other:
            mark_advance_paid(other, other.get(AdvanceRequest, request_id), 'synthetic')
        with pytest.raises(ValueError, match='approved'):
            mark_advance_paid(stale, stale_copy, 'synthetic')
