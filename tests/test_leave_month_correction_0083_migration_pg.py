"""0083: leave_month_corrections. Empty down/up works; a populated table refuses downgrade."""
import os, subprocess, sys
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine, text
from app.db import engine

ROOT = Path(__file__).resolve().parents[1]
PREV = '0082_payroll_carry_deduction'          # payroll carry lane; its own down_revision is 0081_payroll_leave_cash_line


def alembic(env, *a):
    return subprocess.run([sys.executable, '-m', 'alembic', *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def has_table(c):
    return c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_name='leave_month_corrections'")).scalar_one()


def test_0083_down_up_and_populated_downgrade_refuses():
    name = 'test_0083_' + uuid4().hex[:10]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
        assert alembic(env, 'upgrade', 'head').returncode == 0
        from alembic.script import ScriptDirectory
        from alembic.config import Config
        scripts = ScriptDirectory.from_config(Config(str(ROOT / 'alembic.ini')))
        assert scripts.get_revision('0083_leave_month_correction').down_revision == PREV
        assert scripts.get_revision(PREV).down_revision == '0081_payroll_leave_cash_line'
        assert scripts.get_current_head() == '0085_late_entry_months'
        eng = create_engine(url)
        with eng.connect() as c:
            assert has_table(c) == 1
        d = alembic(env, 'downgrade', PREV); assert d.returncode == 0, d.stdout + d.stderr
        with eng.connect() as c:
            assert has_table(c) == 0
        u = alembic(env, 'upgrade', 'head'); assert u.returncode == 0, u.stdout + u.stderr
        # a populated table (immutable receipts are the only content that can exist) refuses the downgrade
        with eng.begin() as c:
            c.execute(text("INSERT INTO admin_accounts (username,password_hash,role,active) VALUES ('mig-b','x','system_admin',true)"))
            actor = c.execute(text("SELECT id FROM admin_accounts WHERE username='mig-b'")).scalar_one()
            c.execute(text("INSERT INTO departments (name) VALUES ('Synthetic dept')"))
            dept = c.execute(text("SELECT id FROM departments WHERE name='Synthetic dept'")).scalar_one()
            c.execute(text("INSERT INTO employees (code,full_name,position,hire_date,status,department_id,basic_salary,daily_hours,created_at) "
                           "VALUES ('M83','Synthetic','x','2026-01-01','active',:d,3000,8,now())"), {'d': dept})
            emp = c.execute(text("SELECT id FROM employees WHERE code='M83'")).scalar_one()
            c.execute(text("INSERT INTO leave_month_corrections (id,employee_id,month_start,original_receipt_id,sequence,actor_id,session_id,"
                           "session_version,source_digest,request_digest,plan_json,result_json,result_digest,created_at) "
                           "VALUES (:i,:e,'2026-02-01',:o,1,:a,'s',1,:d,:d,'{}','{}',:d,now())"),
                      {'i': 'a' * 64, 'e': emp, 'o': 'b' * 64, 'a': actor, 'd': 'c' * 64})
        bad = alembic(env, 'downgrade', PREV)
        assert bad.returncode != 0 and 'Refusing downgrade' in bad.stdout + bad.stderr
        with eng.connect() as c:
            assert has_table(c) == 1
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
