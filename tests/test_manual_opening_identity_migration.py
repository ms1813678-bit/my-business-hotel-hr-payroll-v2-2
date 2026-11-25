"""Actual migrated PG constraints, canonical mapping and history preservation."""
from decimal import Decimal
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, DBAPIError
from tests.test_leave_precision_migration import precision_schema, migrate, seed_legacy  # noqa: F401


def test_upgrade_preserves_history_and_enables_only_canonical_balance_types(precision_schema):
    with precision_schema.begin() as db:
        migrate(db, '0067_sync_uid_base_entities')
        seed_legacy(db, Decimal('1.2500000000000000000001'))
        before = db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one()
        employees = db.execute(text('SELECT to_jsonb(t) FROM employees t')).all()
        types = db.execute(text('SELECT id,code,active,sync_uid FROM leave_types ORDER BY id')).all()
        migrate(db, 'head')
        assert db.execute(text('SELECT to_jsonb(t) FROM leave_balance_transactions t')).scalar_one() == before
        assert db.execute(text('SELECT to_jsonb(t) FROM employees t')).all() == employees
        assert db.execute(text('SELECT id,code,active,sync_uid FROM leave_types ORDER BY id')).all() == types
        assert db.execute(text("SELECT count(*) FROM leave_types WHERE code IN ('annual','weekly_rest','official_holiday') AND tracks_balance")).scalar_one() == 3


@pytest.mark.parametrize('variant', ['valid','short_key','upper_key','bad_category','null_digest','bad_digest','upper_digest','adjustment','partial_year','partial_date','ordinary_key'])
def test_opening_identity_shape_and_immutability(precision_schema, variant):
    with precision_schema.begin() as db:
        migrate(db, 'head'); seed_legacy(db, Decimal('1'))
        emp, kind = db.execute(text('SELECT employee_id,leave_type_id FROM leave_balance_transactions')).one()
        p = dict(e=emp,k=kind,op='manual_opening:'+'a'*64+':A',digest='b'*64,year=None,release=None,tx='opening')
        if variant == 'short_key': p['op']='manual_opening:abc:A'
        elif variant == 'upper_key': p['op']='manual_opening:'+'A'*64+':A'
        elif variant == 'bad_category': p['op']='manual_opening:'+'a'*64+':S'
        elif variant == 'null_digest': p['digest']=None
        elif variant == 'bad_digest': p['digest']='z'*64
        elif variant == 'upper_digest': p['digest']='B'*64
        elif variant == 'adjustment': p['tx']='adjustment'
        elif variant == 'partial_year': p['year']=2026
        elif variant == 'partial_date': p['release']='2026-01-01'
        elif variant == 'ordinary_key': p['op']='ordinary-explicit-accrual'
        stmt=text('''INSERT INTO leave_balance_transactions(employee_id,leave_type_id,transaction_type,days_delta,reason,created_by,created_at,source_operation_key,source_digest,earning_year,released_on)
            VALUES(:e,:k,:tx,2,'Synthetic','synthetic',now(),:op,:digest,:year,:release) RETURNING id''')
        if variant != 'valid':
            with pytest.raises(IntegrityError), db.begin_nested(): db.execute(stmt,p)
            return
        row=db.execute(stmt,p).scalar_one()
        for sql in ('UPDATE leave_balance_transactions SET days_delta=4 WHERE id=:i','DELETE FROM leave_balance_transactions WHERE id=:i'):
            with pytest.raises(DBAPIError, match='immutable'), db.begin_nested():db.execute(text(sql),dict(i=row))
        with pytest.raises(RuntimeError,match='identity would be lost'):
            migrate(db,'0067_sync_uid_base_entities',down=True)
        assert db.execute(text('SELECT version_num FROM alembic_version')).scalar_one()=='0068_manual_opening_identity'
        assert db.execute(text('SELECT days_delta FROM leave_balance_transactions WHERE id=:i'),dict(i=row)).scalar_one()==2


@pytest.mark.parametrize('variant', ['valid','short_key','upper_key','bad_category','null_digest','bad_digest','upper_digest','adjustment','partial_year','partial_date','ordinary_key'])
def test_model_identity_check_is_strict_on_sqlite_metadata(variant):
    import sqlite3
    from app.models import LeaveBalanceTransaction
    constraint = next(c for c in LeaveBalanceTransaction.__table__.constraints
                      if c.name == 'ck_leave_ledger_source_identity')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE probe (transaction_type TEXT, source_operation_key TEXT, source_digest TEXT, earning_year INTEGER, released_on TEXT, CHECK ('+str(constraint.sqltext)+'))')
        values = ['opening', 'manual_opening:'+'a'*64+':A', 'b'*64, None, None]
        if variant == 'short_key': values[1]='manual_opening:abc:A'
        elif variant == 'upper_key': values[1]='manual_opening:'+'A'*64+':A'
        elif variant == 'bad_category': values[1]='manual_opening:'+'a'*64+':S'
        elif variant == 'null_digest': values[2]=None
        elif variant == 'bad_digest': values[2]='z'*64
        elif variant == 'upper_digest': values[2]='B'*64
        elif variant == 'adjustment': values[0]='adjustment'
        elif variant == 'partial_year': values[3]=2026
        elif variant == 'partial_date': values[4]='2026-01-01'
        elif variant == 'ordinary_key': values[1]='ordinary-explicit-accrual'
        if variant == 'valid':
            db.execute('INSERT INTO probe VALUES (?,?,?,?,?)', values)
        else:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute('INSERT INTO probe VALUES (?,?,?,?,?)', values)
