"""Migration 0035 downgrade must never delete named accounts (scratch PostgreSQL database)."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from app.db import engine


def run_alembic(env, root, *args):
    return subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=600)


def test_account_roles_downgrade_refuses_with_two_accounts_and_keeps_them():
    name = 'test_account_roles_mig_' + uuid4().hex[:12]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    root = Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
        up = run_alembic(env, root, 'upgrade', 'head')
        assert up.returncode == 0, up.stdout + up.stderr
        scratch = create_engine(url)
        with scratch.begin() as db:
            db.execute(text(
                "INSERT INTO admin_accounts (username, password_hash, role, active, created_at, session_version, "
                "must_change_password, pending_credential_reset) "
                "VALUES ('synthetic-first','x','system_admin',true,now(),1,false,false),"
                "('synthetic-second','x','finance',true,now(),1,false,false)"))
        # Call this one migration's downgrade directly: older downgrades (sync_uid) refuse first on a full chain.
        import importlib.util
        import pytest
        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext
        path = root / 'alembic' / 'versions' / '0035_account_roles.py'
        spec = importlib.util.spec_from_file_location('mig0035', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with scratch.connect() as conn:
            transaction = conn.begin()
            with Operations.context(MigrationContext.configure(conn)):
                with pytest.raises(RuntimeError, match='named accounts would be deleted'):
                    module.downgrade()
            transaction.rollback()
        with scratch.begin() as db:
            assert db.scalar(text("SELECT count(*) FROM admin_accounts WHERE username LIKE 'synthetic-%'")) == 2
        scratch.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
