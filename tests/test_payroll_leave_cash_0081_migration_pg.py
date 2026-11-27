"""0081: payroll_items.leave_cash_amount. Empty/zero rows migrate down and up; a non-zero line refuses downgrade."""
import os, subprocess, sys
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine, text
from app.db import engine

ROOT = Path(__file__).resolve().parents[1]


def alembic(env, *a):
    return subprocess.run([sys.executable, '-m', 'alembic', *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def has_col(c):
    return c.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name='payroll_items' AND column_name='leave_cash_amount'")).scalar_one()


def test_0081_down_up_and_refusal_with_nonzero_line():
    name = 'test_0081_' + uuid4().hex[:10]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
        assert alembic(env, 'upgrade', 'head').returncode == 0
        eng = create_engine(url)
        with eng.connect() as c:
            assert has_col(c) == 1
        d = alembic(env, 'downgrade', '0080_restore_epoch_rebind'); assert d.returncode == 0, d.stdout + d.stderr
        with eng.connect() as c:
            assert has_col(c) == 0
        u = alembic(env, 'upgrade', 'head'); assert u.returncode == 0, u.stdout + u.stderr
        with eng.begin() as c:
            c.execute(text("INSERT INTO payroll_runs (period_start,period_end,pay_date,status,created_by,created_at) VALUES ('2026-03-01','2026-03-31','2026-03-31','draft','t',now())"))
            run = c.execute(text("SELECT max(id) FROM payroll_runs")).scalar_one()
            c.execute(text("INSERT INTO departments (name) VALUES ('Synthetic dept')"))
            dept = c.execute(text("SELECT id FROM departments WHERE name='Synthetic dept'")).scalar_one()
            c.execute(text("INSERT INTO employees (code,full_name,position,hire_date,status,department_id,basic_salary,daily_hours,created_at) VALUES ('M81','Synthetic','x','2026-01-01','active',:d,3000,8,now())"), {'d': dept})
            emp = c.execute(text("SELECT id FROM employees WHERE code='M81'")).scalar_one()
            c.execute(text("INSERT INTO payroll_items (payroll_run_id,employee_id,employee_code_snapshot,employee_name_snapshot,department_name_snapshot,"
                           "position_snapshot,salary_basis,base_salary,daily_hours,leave_cash_amount,created_at,updated_at) VALUES (:r,:e,'M81','Synthetic','S','x',3000,3000,8,68.58,now(),now())"),
                      {'r': run, 'e': emp})
        bad = alembic(env, 'downgrade', '0080_restore_epoch_rebind')
        assert bad.returncode != 0 and 'Refusing downgrade' in bad.stdout + bad.stderr
        with eng.connect() as c:
            assert has_col(c) == 1
            assert c.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0084_sync_change_log'  # head after the carry migration (all-or-nothing downgrade)
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
