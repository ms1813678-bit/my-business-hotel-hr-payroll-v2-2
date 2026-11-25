"""Characterization on real PostgreSQL: where a non-finite or huge Decimal amount stops.

Describes current behaviour only; not a policy change. Synthetic employee, nothing committed.
"""
import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app import advance_workflow
from app.models import AdvanceRequest, Employee
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


class _Day(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 20)


def _create(db, employee_id, amount, kind='installment'):
    return advance_workflow.create_advance(db, employee_id=employee_id, request_date=datetime.date(2026, 11, 20),
        amount=amount, reason='synthetic', advance_type=kind, actor='tester')


@pytest.mark.parametrize('text', ['Infinity', 'NaN', 'sNaN', '1E400'])
def test_installment_advance_nonfinite_or_huge_never_persists_on_postgres(history_context, monkeypatch, text):  # noqa: F811
    db, _actor, _auth, fact = history_context
    monkeypatch.setattr(advance_workflow, 'date', _Day)
    employee = db.get(Employee, fact.employee_id)
    employee.status = 'active'
    db.flush()
    before = db.scalar(select(func.count()).select_from(AdvanceRequest))
    amount = Decimal(text)
    # Outcome is reported, not assumed: Infinity passes the Python guards and is stopped by the database.
    try:
        with db.begin_nested():
            _create(db, employee.id, amount)
            db.flush()
        outcome = 'persisted'
    except SQLAlchemyError as exc:
        outcome = 'database:' + type(exc.orig).__name__ if getattr(exc, 'orig', None) else 'database:' + type(exc).__name__
    except (ValueError, ArithmeticError) as exc:
        outcome = 'python:' + type(exc).__name__
    assert outcome == {'Infinity': 'database:NumericValueOutOfRange', 'NaN': 'python:InvalidOperation',
        'sNaN': 'python:InvalidOperation', '1E400': 'database:NumericValueOutOfRange'}[text]
    assert db.scalar(select(func.count()).select_from(AdvanceRequest)) == before
