"""The suite guard must reject databases that only LOOK migrated.

A create_all-built database stamped with the head revision passes a naive
alembic_version check but lacks objects the real migration chain creates
(the audit-log immutability triggers). These tests run a real subprocess
pytest against such databases and require the guard to fail the run.
"""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.db import engine as configured_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ADMIN_URL = str(configured_engine.url).rsplit("/", 1)[0] + "/postgres"


def _make_db(name: str) -> str:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    return str(configured_engine.url).rsplit("/", 1)[0] + f"/{name}"


def _drop_db(name: str) -> None:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def _run_guarded_suite(db_url: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env.setdefault("SESSION_SECRET", "test-secret-0123456789abcdef0123456789")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_health.py", "-p", "no:cacheprovider"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


@pytest.mark.skipif(configured_engine.dialect.name != "postgresql", reason="PostgreSQL-only guard")
def test_guard_rejects_stamped_create_all_database():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    name = "guard_stamped_" + uuid4().hex[:12]
    db_url = _make_db(name)
    try:
        import app.models  # noqa: F401  (register every table on Base.metadata)
        from app.db import Base
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        head = ScriptDirectory.from_config(config).get_current_head()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})
        engine.dispose()

        result = _run_guarded_suite(db_url)
        assert result.returncode != 0, "stamped create_all database must not pass the guard"
        assert "trg_audit_logs" in result.stdout + result.stderr
    finally:
        _drop_db(name)


@pytest.mark.skipif(configured_engine.dialect.name != "postgresql", reason="PostgreSQL-only guard")
def test_guard_rejects_database_missing_trigger():
    name = "guard_notrigger_" + uuid4().hex[:12]
    db_url = _make_db(name)
    try:
        env = dict(os.environ)
        env["DATABASE_URL"] = db_url
        env.setdefault("SESSION_SECRET", "test-secret-0123456789abcdef0123456789")
        subprocess.run(
            [sys.executable, "-m", "scripts.initialize_database"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300, check=True,
        )
        engine = create_engine(db_url)
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs"))
        engine.dispose()

        result = _run_guarded_suite(db_url)
        assert result.returncode != 0, "a database missing a trigger must not pass the guard"
        assert "trg_audit_logs_immutable" in result.stdout + result.stderr
    finally:
        _drop_db(name)


def _migrated_db(name: str) -> str:
    db_url = _make_db(name)
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env.setdefault("SESSION_SECRET", "test-secret-0123456789abcdef0123456789")
    subprocess.run(
        [sys.executable, "-m", "scripts.initialize_database"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300, check=True,
    )
    return db_url


@pytest.mark.skipif(configured_engine.dialect.name != "postgresql", reason="PostgreSQL-only guard")
def test_guard_rejects_disabled_trigger():
    name = "guard_disabled_" + uuid4().hex[:12]
    db_url = _migrated_db(name)
    try:
        engine = create_engine(db_url)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_immutable"))
        engine.dispose()
        result = _run_guarded_suite(db_url)
        assert result.returncode != 0, "a disabled trigger must not pass the guard"
        assert "trg_audit_logs_immutable" in result.stdout + result.stderr
    finally:
        _drop_db(name)


@pytest.mark.skipif(configured_engine.dialect.name != "postgresql", reason="PostgreSQL-only guard")
def test_guard_rejects_same_named_ineffective_trigger():
    name = "guard_noop_" + uuid4().hex[:12]
    db_url = _migrated_db(name)
    try:
        engine = create_engine(db_url)
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER trg_audit_logs_immutable ON audit_logs"))
            conn.execute(text(
                "CREATE OR REPLACE FUNCTION noop_audit_trigger() RETURNS trigger AS $$ "
                "BEGIN RETURN NULL; END; $$ LANGUAGE plpgsql"
            ))
            conn.execute(text(
                "CREATE TRIGGER trg_audit_logs_immutable BEFORE UPDATE OR DELETE ON audit_logs "
                "FOR EACH ROW EXECUTE FUNCTION noop_audit_trigger()"
            ))
        engine.dispose()
        result = _run_guarded_suite(db_url)
        assert result.returncode != 0, "a same-named ineffective trigger must not pass the guard"
        assert "did not block" in result.stdout + result.stderr
    finally:
        _drop_db(name)
