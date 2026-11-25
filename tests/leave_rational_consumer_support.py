"""Fixtures and helpers for tests/test_leave_rational_consumers.py.

Built on the real, existing fixture chain (tests.test_department_workbook_store.context
-> tests.test_leave_historical_evidence.history_context), so identity, session and
restore epoch are the actual ones the application guards use. Real migrated
PostgreSQL rows only, no mocks. The chain rolls the session back at teardown.
"""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.models import Employee, LeaveBalanceTransaction, LeaveType
from tests.test_department_workbook_store import context  # noqa: F401  (fixture dependency)
from tests.test_leave_historical_evidence import history_context  # noqa: F401

ONE_SEVENTH = Fraction(1, 7)
# 36 places with a trailing zero: PG numeric keeps the stored scale text.
HIGH_SCALE_MANUAL = Decimal('0.123456789123456789123456789123456780')
MIXED_FINITE = Decimal('1.000000000000000000000000000000000001')
PLAIN_FINITE = Decimal('2.50')


def ledger_row(employee, kind, transaction_type='adjustment', **changes):
    return LeaveBalanceTransaction(
        employee_id=employee.id, leave_type_id=kind.id, transaction_type=transaction_type,
        reason='Synthetic exact row', created_by='synthetic', **changes)


def rational_row(employee, kind, fraction, **extra):
    return ledger_row(employee, kind, days_delta=None,
                      exact_numerator=Decimal(fraction.numerator),
                      exact_denominator=Decimal(fraction.denominator), **extra)


def finite_row(employee, kind, amount, **extra):
    return ledger_row(employee, kind, days_delta=amount, **extra)


def _kind(code, tracks):
    return LeaveType(code=code, name_ar='Synthetic', name_en='Synthetic',
                     tracks_balance=tracks, active=True, is_paid=True)


@dataclass
class ConsumerStore:
    db: object
    auth: dict                 # actor_id, session_id, session_version, expected_epoch (real)
    employee: object
    rational_kind: object      # tracking: exactly one 1/7 row
    manual_kind: object        # tracking: exactly one high-scale finite row
    mixed_kind: object         # tracking: 1/7 row + finite row
    plain_kind: object         # NON-tracking: 2.50
    annual_kind: object        # canonical 'annual' type, no rows yet
    rational_row: object
    manual_row: object
    mixed_rational_row: object
    mixed_finite_row: object
    plain_row: object


@pytest.fixture
def consumer_store(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    employee = db.get(Employee, fact.employee_id)
    rational_kind, manual_kind, mixed_kind, plain_kind = (
        _kind('syn-rc-rat', True), _kind('syn-rc-man', True),
        _kind('syn-rc-mix', True), _kind('syn-rc-plain', False))
    db.add_all([rational_kind, manual_kind, mixed_kind, plain_kind])
    db.flush()
    annual_kind = db.scalar(select(LeaveType).where(LeaveType.code == 'annual'))
    assert annual_kind is not None and annual_kind.tracks_balance
    rows = dict(
        rational_row=rational_row(employee, rational_kind, ONE_SEVENTH),
        manual_row=finite_row(employee, manual_kind, HIGH_SCALE_MANUAL),
        mixed_rational_row=rational_row(employee, mixed_kind, ONE_SEVENTH),
        mixed_finite_row=finite_row(employee, mixed_kind, MIXED_FINITE),
        plain_row=finite_row(employee, plain_kind, PLAIN_FINITE),
    )
    db.add_all(rows.values())
    db.flush()
    return ConsumerStore(db, auth, employee, rational_kind, manual_kind, mixed_kind,
                         plain_kind, annual_kind, **rows)
