"""Normal named migrated database, real auth, no synthetic operation flags."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.db import engine


@pytest.mark.parametrize('category',['W','A'])
@pytest.mark.parametrize('language',['ar','en','zh'])
def test_normal_expiry_http(tmp_path,language,category):
    name = 'test_expiry_http_' + uuid4().hex[:12]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    root = Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        env = {**os.environ, 'DATABASE_URL': engine.url.set(database=name).render_as_string(hide_password=False), 'HR_EXPIRY_LANGUAGE':language,'HR_EXPIRY_CATEGORY':category}
        if os.environ.get('HR_PIXEL_DIR'):
            env['HR_PIXEL_DIR']=str(Path(os.environ['HR_PIXEL_DIR'])/language)
        for args in [['-m', 'alembic', 'upgrade', 'head'],
                     ['-m', 'pytest', 'tests/carry_expiry_http_acceptance.py', '-q', '--tb=short',
                      '--junitxml=' + str(tmp_path / 'http.xml')]]:
            result = subprocess.run([sys.executable, *args], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=300)
            assert result.returncode == 0, result.stdout + result.stderr
        from xml.etree import ElementTree
        suites = list(ElementTree.parse(tmp_path / 'http.xml').getroot().iter('testsuite'))
        assert suites and sum(int(x.attrib['tests']) for x in suites) == 1
        assert all(int(x.attrib.get(k, '0')) == 0 for x in suites for k in ('errors', 'failures', 'skipped'))
    finally:
        with admin.connect() as c:
            c.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()'), dict(n=name))
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
