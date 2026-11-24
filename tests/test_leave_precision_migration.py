"""Real PostgreSQL precision migration preservation and safe rollback tests."""
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from app.db import engine


@pytest.fixture
def precision_schema():
    schema = 'leave_precision_' + uuid4().hex
    with engine.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = create_engine(engine.url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        yield isolated
    finally:
        isolated.dispose()
        with engine.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def migrate(connection, revision, *, down=False):
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    config.attributes['connection'] = connection
    (command.downgrade if down else command.upgrade)(config, revision)


def seed_legacy(connection, amount):
    department = connection.execute(text(
        "INSERT INTO departments(name,active) VALUES ('Synthetic precision',true) RETURNING id"
    )).scalar_one()
    employee = connection.execute(text("""
        INSERT INTO employees(code,full_name,department_id,position,hire_date,status,
                              basic_salary,daily_hours,created_at)
        VALUES ('SYN-PRECISION','Synthetic',:department,'Synthetic','2026-01-01',
                'active',123.45,8,now()) RETURNING id
    """), {'department': department}).scalar_one()
    leave_type = connection.execute(text("""
        INSERT INTO leave_types(code,name_ar,name_en,is_paid,tracks_balance,active)
        VALUES ('synprec','Synthetic','Synthetic',true,true,true) RETURNING id
    """)).scalar_one()
    connection.execute(text("""
        INSERT INTO leave_balance_transactions(employee_id,leave_type_id,transaction_type,
                        days_delta,reason,created_by,created_at)
        VALUES (:employee,:leave_type,'adjustment',:amount,'Synthetic','synthetic',now())
    """), {'employee': employee, 'leave_type': leave_type, 'amount': amount})


def test_0044_preserves_legacy_days_and_employee_fields_and_safe_downgrade(precision_schema):
    with precision_schema.begin() as db:
        migrate(db, '0043_leave_close_metadata')
        seed_legacy(db, Decimal('12.34'))
        before = db.execute(text('SELECT to_jsonb(t) FROM employees t')).scalar_one()
        migrate(db, '0044_leave_exact_day_storage')
        assert db.execute(text('SELECT days_delta FROM leave_balance_transactions')).scalar_one() == Decimal('12.34')
        assert db.execute(text('SELECT to_jsonb(t) FROM employees t')).scalar_one() == before
        migrate(db, '0043_leave_close_metadata', down=True)
        assert db.execute(text('SELECT days_delta FROM leave_balance_transactions')).scalar_one() == Decimal('12.34')


@pytest.mark.parametrize('amount', [Decimal('0.142857142857142857142857142857'), Decimal('100000.00')])
def test_0044_refuses_lossy_downgrade_without_mutating_revision(precision_schema, amount):
    with precision_schema.begin() as db:
        migrate(db, '0044_leave_exact_day_storage')
        seed_legacy(db, amount)
        with pytest.raises(RuntimeError, match='would round or overflow'):
            migrate(db, '0043_leave_close_metadata', down=True)
        assert db.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0044_leave_exact_day_storage'
        assert db.execute(text('SELECT days_delta FROM leave_balance_transactions')).scalar_one() == amount
