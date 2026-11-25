"""Migration 0072 on a scratch database: clean upgrade, empty downgrade,
populated downgrade refusal. Follows test_month_earning_http_pg.py's pattern.
"""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from app.db import engine


def run_alembic(env, root, *args):
    return subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=300)


def test_cash_out_migration_upgrade_downgrade_populated_refusal(tmp_path):
    name = 'test_cash_out_mig_' + uuid4().hex[:12]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    root = Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        env = {**os.environ, 'DATABASE_URL': str(engine.url.set(database=name))}
        up = run_alembic(env, root, 'upgrade', 'head')
        assert up.returncode == 0, up.stdout + up.stderr
        # Empty downgrade of 0072 succeeds, and re-upgrade is clean.
        down = run_alembic(env, root, 'downgrade', '0073_leave_year_attribution')
        assert down.returncode == 0, down.stdout + down.stderr
        up2 = run_alembic(env, root, 'upgrade', 'head')
        assert up2.returncode == 0, up2.stdout + up2.stderr
        # Populate one immutable receipt, then the same downgrade must refuse.
        pop = subprocess.run([sys.executable, '-m', 'pytest', 'tests/cash_out_migration_acceptance.py',
                              '-q', '--tb=short'], cwd=root, env=env,
                             capture_output=True, text=True, timeout=300)
        assert pop.returncode == 0, pop.stdout + pop.stderr
        refused = run_alembic(env, root, 'downgrade', '0073_leave_year_attribution')
        assert refused.returncode != 0
        assert 'Refusing downgrade' in refused.stdout + refused.stderr
        # The database is still at head and usable after the refused downgrade.
        current = run_alembic(env, root, 'current')
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        assert ScriptDirectory.from_config(Config(str(root/'alembic.ini'))).get_current_head() in current.stdout + current.stderr
    finally:
        with admin.connect() as c:
            c.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()'), dict(n=name))
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
