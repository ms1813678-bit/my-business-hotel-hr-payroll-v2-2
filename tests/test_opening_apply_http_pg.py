"""Committed HTTP acceptance runs only in its own disposable migrated database.

The child contains four actual application tests; no fake auth or create_all.
"""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import create_engine, text
from app.db import engine


def test_disposable_actual_http_login_csrf_apply_locales_and_responsiveness(tmp_path):
    name='test_opening_http_'+uuid4().hex[:12]
    admin=create_engine(engine.url.set(database='postgres'),isolation_level='AUTOCOMMIT')
    root=Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
        env={**os.environ,'DATABASE_URL':engine.url.set(database=name).render_as_string(hide_password=False),'APP_ENV':'test'}
        migration=subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert migration.returncode==0,migration.stdout+migration.stderr
        run=subprocess.run([sys.executable,'-m','pytest','tests/opening_http_acceptance.py','-q','--tb=short','--junitxml='+str(tmp_path/'http.xml')],cwd=root,env=env,capture_output=True,text=True,timeout=90)
        assert run.returncode==0,run.stdout+run.stderr
        from xml.etree import ElementTree
        report=ElementTree.parse(tmp_path/'http.xml')
        suites=list(report.getroot().iter('testsuite'))
        assert suites and sum(int(x.attrib['tests']) for x in suites)>=4
        assert all(int(x.attrib.get(k,'0'))==0 for x in suites for k in ('errors','failures','skipped'))
    finally:
        with admin.connect() as c:
            c.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()'),dict(n=name))
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
