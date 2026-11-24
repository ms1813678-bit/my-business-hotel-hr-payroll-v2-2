"""Reconciled exact/negative legacy path, synthetic migrated PostgreSQL."""
from datetime import date
from decimal import Decimal, localcontext

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.leave_workflow import add_balance_entry, cancel_leave, confirm_leave, leave_balance
from app.models import LeaveBalanceTransaction, LeaveRecord, LeaveType
from tests.test_leave_precision_migration import migrate, precision_schema, seed_legacy  # noqa: F401


def test_0047_legacy_rows_preserved_and_safe_downgrade(precision_schema):  # noqa: F811
    with precision_schema.begin() as connection:
        migrate(connection, '0046_leave_source_immutable')
        seed_legacy(connection, Decimal('1.00'))
        employee_id, type_id = connection.execute(text(
            'SELECT employee_id,leave_type_id FROM leave_balance_transactions')).one()
        connection.execute(text('''INSERT INTO leave_records(employee_id,leave_type_id,
            start_date,end_date,calendar_days,charged_days,reason,status,created_by,created_at)
            VALUES (:e,:t,'2026-01-01','2026-01-02',2,1.25,'Synthetic','draft','synthetic',now())'''),
            {'e': employee_id, 't': type_id})
        before = connection.execute(text('SELECT to_jsonb(t) FROM leave_records t')).scalar_one()
        migrate(connection, '0047_leave_request_exact_days')
        assert connection.execute(text('SELECT to_jsonb(t) FROM leave_records t')).scalar_one() == before
        migrate(connection, '0046_leave_source_immutable', down=True)
        assert connection.execute(text('SELECT to_jsonb(t) FROM leave_records t')).scalar_one() == before


def test_exact_use_negative_adjustment_and_reversal_preserve_fraction(precision_schema):  # noqa: F811
    with precision_schema.begin() as connection:
        migrate(connection, 'head')
        seed_legacy(connection, Decimal(1))
    with Session(precision_schema) as db, db.begin():
        original = db.scalar(select(LeaveBalanceTransaction))
        kind = db.get(LeaveType, original.leave_type_id)
        amount = Decimal('1.14285714285714285714285714285714285714285714285714')
        record = LeaveRecord(employee_id=original.employee_id, leave_type_id=kind.id,
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), calendar_days=2,
            charged_days=amount, reason='Synthetic precise request', status='draft', created_by='synthetic')
        db.add(record)
        db.flush()
        db.refresh(record)
        assert record.charged_days == amount
        with localcontext() as context:
            context.prec = 4
            confirm_leave(db, record, 'synthetic')
        use = db.scalar(select(LeaveBalanceTransaction).where(
            LeaveBalanceTransaction.transaction_type == 'leave_use'))
        assert use.days_delta == amount.copy_negate()
        assert leave_balance(db, original.employee_id, kind.id) < 0
        cancel_leave(db, record, 'synthetic', 'Synthetic reversal')
        db.flush()
        assert leave_balance(db, original.employee_id, kind.id) == Decimal(1)
        add_balance_entry(db, employee_id=original.employee_id, leave_type=kind,
            transaction_type='adjustment', days_delta=Decimal('-3.12345678901234567890123456789'),
            reason='Synthetic negative correction', actor='synthetic')
        assert leave_balance(db, original.employee_id, kind.id) == Decimal('-2.12345678901234567890123456789')
        with pytest.raises(RuntimeError, match='would round'):
            migrate(db.connection(), '0046_leave_source_immutable', down=True)


def test_ai_review_uses_same_exact_negative_rule(precision_schema, monkeypatch):  # noqa: F811
    from sqlalchemy.orm import sessionmaker

    import app.ai_gateway as gateway

    with precision_schema.begin() as connection:
        migrate(connection, 'head')
        seed_legacy(connection, Decimal(1))
    factory = sessionmaker(precision_schema)
    with factory() as db:
        row = db.scalar(select(LeaveBalanceTransaction))
        employee_id, type_id = row.employee_id, row.leave_type_id
    monkeypatch.setattr(gateway, 'SessionLocal', factory)
    delta = Decimal('-2.1234567890123456789012345678901234567890123456789')
    with localcontext() as context:
        context.prec = 4
        preview = gateway.build_confirmation_preview({'action': 'adjust_leave_balance',
            'employee_id': employee_id, 'leave_type_id': type_id,
            'balance_days': str(delta), 'transaction_type': 'adjustment', 'reason': 'Synthetic'})
    assert Decimal(preview['new_values']['days_delta']) == delta
    assert preview['new_values']['resulting_balance'] == '-1.1234567890123456789012345678901234567890123456789'


def test_ai_preview_exponent_distance_and_negative_opening_match_writer(precision_schema, monkeypatch):  # noqa: F811
    from sqlalchemy.orm import sessionmaker

    import app.ai_gateway as gateway

    with precision_schema.begin() as connection:
        migrate(connection, 'head')
        seed_legacy(connection, Decimal(1))
    factory = sessionmaker(precision_schema)
    with factory() as db:
        row = db.scalar(select(LeaveBalanceTransaction))
        employee_id, type_id = row.employee_id, row.leave_type_id
    monkeypatch.setattr(gateway, 'SessionLocal', factory)
    plan = {'action': 'adjust_leave_balance', 'employee_id': employee_id,
            'leave_type_id': type_id, 'balance_days': '-1e-100',
            'transaction_type': 'adjustment', 'reason': 'Synthetic exponent distance'}
    with localcontext() as context:
        context.prec = 4
        preview = gateway.build_confirmation_preview(plan)
    assert preview['new_values']['resulting_balance'] == '0.' + '9' * 100
    assert Decimal(preview['new_values']['days_delta']) == Decimal('-1e-100')
    with pytest.raises(ValueError, match='Negative opening is not permitted'):
        gateway.build_confirmation_preview({**plan, 'transaction_type': 'opening'})
