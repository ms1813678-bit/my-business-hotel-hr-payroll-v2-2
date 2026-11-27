"""A freshly initialised database must hold no business data (Mo, 27 Nov 2026 17:29).

Only three tables may be non-empty after `python -m scripts.initialize_database`:
alembic_version (which schema version the database is at), employee_code_counters
(the next-employee-code counter, one row at zero) and leave_types (the six leave
kinds the system needs: annual, sick, casual, unpaid, official holiday, weekly rest). Anything else is data and fails here.
"""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.db import engine

ALLOWED_NON_EMPTY = {"alembic_version": 1, "employee_code_counters": 1, "leave_types": 6}


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="needs PostgreSQL")
def test_fresh_database_has_only_the_required_system_rows():
    name = "test_fresh_install_" + uuid4().hex[:12]
    admin = create_engine(engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    root = Path(__file__).resolve().parents[1]
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
        done = subprocess.run([sys.executable, "-m", "scripts.initialize_database"], cwd=root, env=env,
                              capture_output=True, text=True, timeout=600)
        assert done.returncode == 0, done.stdout + done.stderr
        fresh = create_engine(url)
        found = {}
        with fresh.connect() as db:
            tables = db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars().all()
            assert len(tables) > 20
            for table in tables:
                count = db.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
                if count:
                    found[table] = count
        fresh.dispose()
        assert found == ALLOWED_NON_EMPTY, found
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
