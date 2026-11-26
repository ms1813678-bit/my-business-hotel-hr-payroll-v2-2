"""0078/0079 downgrade: empty up/down OK; populated downgrade refuses, nothing changes. Per-case scratch DB test_*."""
import os, subprocess, sys
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from app.db import engine

ROOT = Path(__file__).resolve().parents[1]
ZERO = ('leave_zero_decisions', 'prevent_leave_zero_mutation')
EXP = ('leave_carry_expiries', 'prevent_carry_expiry_mutation')


def alembic(env, *a):
    return subprocess.run([sys.executable, '-m', 'alembic', *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def snap(c):
    q = lambda s: [tuple(r) for r in c.execute(text(s)).all()]
    return dict(
        tables=q("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1"),
        indexes=q("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1"),
        triggers=q("SELECT tgname,tgrelid::regclass::text FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1,2"),
        funcs=q("SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' ORDER BY 1"),
        rows=q('SELECT id,ledger_id,request_digest FROM leave_zero_decisions UNION ALL SELECT id,ledger_id,request_digest FROM leave_carry_expiries ORDER BY 1'),
        version=q('SELECT version_num FROM alembic_version'))


def populate(c, table):
    c.execute(text("INSERT INTO departments (name,active) VALUES ('mig-dep',true)"))
    dep = c.execute(text("SELECT id FROM departments WHERE name='mig-dep'")).scalar_one()
    c.execute(text("INSERT INTO employees (code,full_name,department_id,position,hire_date,basic_salary,daily_hours) VALUES ('MIG1','Synthetic',:d,'p','2025-01-01',1,8)"), {'d': dep})
    emp = c.execute(text("SELECT id FROM employees WHERE code='MIG1'")).scalar_one()
    lt = c.execute(text('SELECT id FROM leave_types ORDER BY id LIMIT 1')).scalar_one()
    c.execute(text("INSERT INTO leave_balance_transactions (employee_id,leave_type_id,transaction_type,days_delta,reason,created_by,created_at) VALUES (:e,:t,'adjustment',-1,'Synthetic','mig',now())"), {'e': emp, 't': lt})
    led = c.execute(text('SELECT max(id) FROM leave_balance_transactions')).scalar_one()
    if table == 'leave_zero_decisions':
        c.execute(text("INSERT INTO leave_zero_decisions (id,employee_id,leave_type_id,source_year,ledger_id,request_digest,source_digest,result_digest,request_json,result_json,created_at) VALUES (:i,:e,:t,2025,:l,:d,:d,:d,'{}','{}',now())"),
                  {'i': 'a' * 64, 'e': emp, 't': lt, 'l': led, 'd': 'b' * 64})
    else:
        c.execute(text("INSERT INTO leave_carry_expiries (id,employee_id,leave_type_id,grant_kind,grant_id,ledger_id,excluded_canonical,source_digest,request_json,request_digest,result_json,result_digest,created_at) VALUES (:i,:e,:t,'carry',:g,:l,'1/1',:d,'{}',:d,'{}',:d,now())"),
                  {'i': 'a' * 64, 'e': emp, 't': lt, 'g': 'c' * 64, 'l': led, 'd': 'b' * 64})


@pytest.mark.parametrize('populated', [None, ZERO, EXP])
def test_0078_0079_downgrade(populated):
    name = 'test_0078_79_down_' + uuid4().hex[:8]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
        assert alembic(env, 'upgrade', '0080_restore_epoch_rebind').returncode == 0
        eng = create_engine(url)
        if populated is None:   # compatible empty path: 0080 -> 0077 -> head
            d = alembic(env, 'downgrade', '-3'); assert d.returncode == 0, d.stdout + d.stderr
            with eng.connect() as c:
                t = [x for (x,) in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).all()]
                assert 'leave_zero_decisions' not in t and 'leave_carry_expiries' not in t
                assert not c.execute(text("SELECT 1 FROM pg_proc WHERE proname IN ('prevent_leave_zero_mutation','prevent_carry_expiry_mutation')")).all()
            u = alembic(env, 'upgrade', '0080_restore_epoch_rebind'); assert u.returncode == 0, u.stdout + u.stderr
            return
        with eng.begin() as c:
            populate(c, populated[0])
        with eng.connect() as c:
            before = snap(c)
        assert before['version'] == [('0080_restore_epoch_rebind',)] and len(before['rows']) == 1
        bad = alembic(env, 'downgrade', '-3')   # through 0080, 0079, 0078 in one run
        assert bad.returncode != 0 and 'Refusing downgrade' in bad.stdout + bad.stderr, bad.stdout + bad.stderr
        with eng.connect() as c:
            assert snap(c) == before            # rows, tables, indexes, triggers, functions, version all unchanged (whole run rolled back)
        with eng.begin() as c:                   # immutability triggers still live
            with pytest.raises(DBAPIError):
                with c.begin_nested():
                    c.execute(text(f'UPDATE {populated[0]} SET request_json=request_json'))
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
