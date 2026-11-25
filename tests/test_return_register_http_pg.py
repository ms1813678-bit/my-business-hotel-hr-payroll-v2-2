"""Committed HTTP acceptance runs only in its own disposable migrated database.

Each locale child runs one actual application test; no fake auth or create_all.
"""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text
from app.db import engine


import pytest

@pytest.mark.parametrize("lang",["ar","en","zh"])
def test_disposable_normal_database_reviewed_register_http(tmp_path,lang):
    name='test_return_http_'+uuid4().hex[:12]
    admin=create_engine(engine.url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    root=Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
        env={**os.environ,'DATABASE_URL':engine.url.set(database=name).render_as_string(hide_password=False),'APP_ENV':'test','REGISTER_ACCEPTANCE_LANG':lang}
        migration=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert migration.returncode==0,migration.stdout+migration.stderr
        # Empty normal-receipt migration reverses cleanly, then recreates the real objects.
        down=subprocess.run([sys.executable,'-m','alembic','downgrade','0068_manual_opening_identity'],cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert down.returncode==0,down.stdout+down.stderr
        again=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert again.returncode==0,again.stdout+again.stderr
        run=subprocess.run([sys.executable,'-m','pytest','tests/register_http_acceptance.py','-q','--tb=short','--junitxml='+str(tmp_path/'http.xml')],cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert run.returncode==0,run.stdout+run.stderr
        from xml.etree import ElementTree
        report=ElementTree.parse(tmp_path/'http.xml')
        suites=list(report.getroot().iter('testsuite'))
        assert suites and sum(int(x.attrib['tests']) for x in suites)==1
        assert all(int(x.attrib.get(k,'0'))==0 for x in suites for k in ('errors','failures','skipped'))
        if os.name=='posix':
            refused=subprocess.run([sys.executable,'-m','alembic','downgrade','0068_manual_opening_identity'],cwd=root,env=env,capture_output=True,text=True,timeout=90)
            assert refused.returncode!=0 and 'register consumption evidence would be lost' in refused.stderr
            target=create_engine(engine.url.set(database=name))
            with target.connect() as check:
                assert check.scalar(text('SELECT count(*) FROM leave_register_consumptions'))==1
                assert check.scalar(text('SELECT version_num FROM alembic_version'))=='0071_leave_month_earning'
            target.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()'),dict(n=name))
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
