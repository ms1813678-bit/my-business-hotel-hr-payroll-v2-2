"""Self-contained wrapper: every case gets its OWN disposable database test_upload_race_<random> (alembic upgrade head),
runs the inner case in a subprocess pytest against it, and always drops it. Never touches a shared DB."""
import os, subprocess, sys
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from app.db import engine

ROOT = Path(__file__).resolve().parents[1]
CASES = [f'tests/attendance_upload_race_inner.py::{n}[{s}]' for n in ('test_service_race_one_batch_clean_valueerror_and_callers_pending_work_kept', 'test_route_double_submit_loser_400_not_500') for s in ('program_report', 'fingerprint')]


@pytest.mark.parametrize('case', CASES)
def test_duplicate_upload_race_case(case):
    if engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL required')
    name = 'test_upload_race_' + uuid4().hex[:10]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        env = {**os.environ, 'DATABASE_URL': engine.url.set(database=name).render_as_string(hide_password=False)}
        up = subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        assert up.returncode == 0, up.stdout + up.stderr
        r = subprocess.run([sys.executable, '-m', 'pytest', case, '-q', '-p', 'no:cacheprovider', '--tb=short'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, (r.stdout + r.stderr)[-1800:]
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
