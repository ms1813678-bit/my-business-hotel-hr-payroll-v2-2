"""Real migrated PostgreSQL ledger identity preservation, no posting enabled."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_leave_precision_migration import migrate, precision_schema, seed_legacy  # noqa: F401


def test_source_identity_upgrade_preserves_existing_ledger_and_safe_downgrade(precision_schema):  # noqa: F811
    with precision_schema.begin() as db:
        migrate(db,'0044_leave_exact_day_storage')
        seed_legacy(db,Decimal('12.34'))
        before=db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one()
        migrate(db,'0045_leave_source_identity')
        after=db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one()
        assert {key:after[key] for key in before}==before
        assert after['source_operation_key'] is None
        assert after['earning_year'] is None
        migrate(db,'0044_leave_exact_day_storage',down=True)
        assert db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one()==before


def test_source_identity_requires_complete_metadata_and_unique_operation(precision_schema):  # noqa: F811
    with precision_schema.begin() as db:
        migrate(db,'0045_leave_source_identity')
        seed_legacy(db,Decimal('0.333333333333333333333333333333'))
        with pytest.raises(IntegrityError),db.begin_nested():
            db.execute(text("UPDATE leave_balance_transactions SET source_operation_key='synthetic'") )
        db.execute(text('UPDATE leave_balance_transactions SET source_operation_key=:key, '
            'source_digest=:digest,earning_year=2025,released_on=:day'),
            {'key':'synthetic-earned-2025','digest':'a'*64,'day':date(2026,2,1)})
        # A policy/source revision does not manufacture another operation slot.
        with pytest.raises(IntegrityError),db.begin_nested():
            db.execute(text('INSERT INTO leave_balance_transactions(employee_id,leave_type_id,transaction_type,days_delta,'
                'reason,created_by,created_at,source_operation_key,source_digest,earning_year,released_on) '
                "SELECT employee_id,leave_type_id,transaction_type,days_delta,reason,created_by,created_at,"
                "source_operation_key,:digest,earning_year,released_on FROM leave_balance_transactions"),{'digest':'b'*64})
        with pytest.raises(RuntimeError,match='lose identity'):
            migrate(db,'0044_leave_exact_day_storage',down=True)
        assert db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='0045_leave_source_identity'
        row=db.execute(text('SELECT earning_year,released_on,days_delta FROM leave_balance_transactions')).one()
        assert row.earning_year==2025 and row.released_on==date(2026,2,1)
        assert row.days_delta==Decimal('0.333333333333333333333333333333')


def test_0046_safe_downgrade_retains_legacy_content(precision_schema):  # noqa: F811
    with precision_schema.begin() as db:
        migrate(db, '0045_leave_source_identity')
        seed_legacy(db, Decimal('12.34'))
        before = db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one()
        migrate(db, '0046_leave_source_immutable')
        migrate(db, '0045_leave_source_identity', down=True)
        assert db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one() == before
