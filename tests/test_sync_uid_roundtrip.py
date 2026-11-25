"""Independent populated UID dump/restore and behavioral guard regressions."""
from pathlib import Path
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.db import engine as configured_engine
from tests.test_test_db_guard import _migrated_db, _make_db, _drop_db, _run_guarded_suite
from tests.sync_uid_guard import TABLES, verify_sync_uid_enforcement
from tests.test_sync_uid_migration import seed, snapshot, uids

pytestmark = pytest.mark.skipif(configured_engine.dialect.name != "postgresql", reason="Real PostgreSQL required")


@pytest.mark.parametrize("mode", ["noop_" + t for t in TABLES] + ["check", "unique", "default"])
def test_guard_rejects_same_named_ineffective_uid_objects(mode):
    name = "guard_uid_" + uuid.uuid4().hex[:12]
    url = _migrated_db(name)
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            if mode.startswith("noop_"):
                table = mode.removeprefix("noop_")
                c.execute(text("CREATE FUNCTION uid_noop() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql"))
                c.execute(text(f"DROP TRIGGER trg_{table}_sync_uid_immutable ON {table}"))
                c.execute(text(f"CREATE TRIGGER trg_{table}_sync_uid_immutable BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION uid_noop()"))
            elif mode == "check":
                c.execute(text("ALTER TABLE departments DROP CONSTRAINT ck_departments_sync_uid_v4"))
                c.execute(text("ALTER TABLE departments ADD CONSTRAINT ck_departments_sync_uid_v4 CHECK (true)"))
            elif mode == "unique":
                c.execute(text("ALTER TABLE departments DROP CONSTRAINT uq_departments_sync_uid"))
                c.execute(text("ALTER TABLE departments ADD CONSTRAINT uq_departments_sync_uid UNIQUE (id)"))
            else:
                c.execute(text("ALTER TABLE departments ALTER COLUMN sync_uid SET DEFAULT 'not-a-uuid'"))
        result = _run_guarded_suite(url)
        assert result.returncode != 0
        assert "sync_uid" in result.stdout + result.stderr
    finally:
        engine.dispose()
        _drop_db(name)


def _catalog(c):
    constraints = c.execute(text(
        "SELECT k.relname,p.conname,p.contype,pg_get_constraintdef(p.oid),p.convalidated "
        "FROM pg_constraint p JOIN pg_class k ON k.oid=p.conrelid "
        "WHERE k.relnamespace=current_schema()::regnamespace AND p.conname LIKE '%sync_uid%' ORDER BY 1,2"
    )).all()
    triggers = c.execute(text(
        "SELECT k.relname,g.tgname,g.tgenabled,pg_get_triggerdef(g.oid) FROM pg_trigger g "
        "JOIN pg_class k ON k.oid=g.tgrelid WHERE k.relnamespace=current_schema()::regnamespace "
        "AND g.tgname LIKE '%sync_uid%' ORDER BY 1,2"
    )).all()
    functions = c.execute(text(
        "SELECT proname,pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace=current_schema()::regnamespace "
        "AND proname IN ('sync_uid_new','sync_uid_immutable') ORDER BY 1"
    )).all()
    defaults = c.execute(text(
        "SELECT table_name,column_default,is_nullable FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND column_name='sync_uid' ORDER BY 1"
    )).all()
    return constraints, triggers, functions, defaults


def test_populated_all_four_entities_dump_restore_preserves_identity_and_enforcement(tmp_path):
    suffix = uuid.uuid4().hex[:12]
    source_name, target_name = "uid_dump_" + suffix, "uid_restore_" + suffix
    source_url = _migrated_db(source_name)
    source, target = create_engine(source_url), None
    try:
        with source.begin() as c:
            seed(c)
            c.execute(text("INSERT INTO audit_logs(actor,action,entity_type,entity_id,created_at) VALUES ('synthetic','uid_dump','test','1',now())"))
        with source.connect() as c:
            before, identity, catalog = snapshot(c), uids(c), _catalog(c)
            assert all(identity[t] for t in TABLES)
            assert len(catalog[0]) == 8 and len(catalog[1]) == 4 and len(catalog[2]) == 2 and len(catalog[3]) == 4
            audit = c.execute(text("SELECT to_jsonb(t) FROM audit_logs t ORDER BY id")).scalars().all()
        target_url = _make_db(target_name)
        target = create_engine(target_url)
        from app.backup_workflow import _find_pg_tool
        pg_dump, pg_restore = _find_pg_tool("pg_dump"), _find_pg_tool("pg_restore")
        archive = tmp_path / "populated.dump"
        def cli_url(url):
            from sqlalchemy.engine import make_url
            return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
        subprocess.run([pg_dump, "-Fc", "--no-owner", "--no-acl", "-d", cli_url(source_url), "-f", str(archive)], check=True, capture_output=True)
        subprocess.run([pg_restore, "--no-owner", "--no-acl", "--exit-on-error", "-d", cli_url(target_url), str(archive)], check=True, capture_output=True)
        with target.connect() as c:
            assert snapshot(c) == before and uids(c) == identity and _catalog(c) == catalog
            assert c.execute(text("SELECT to_jsonb(t) FROM audit_logs t ORDER BY id")).scalars().all() == audit
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0068_manual_opening_identity"
            links = c.execute(text("SELECT e.department_id,d.id,r.employee_id,e.id,r.leave_type_id,t.id FROM leave_records r JOIN employees e ON r.employee_id=e.id JOIN departments d ON e.department_id=d.id JOIN leave_types t ON r.leave_type_id=t.id")).one()
            assert links[0] == links[1] and links[2] == links[3] and links[4] == links[5]
        verify_sync_uid_enforcement(target)
        result = _run_guarded_suite(target_url)
        assert result.returncode == 0, result.stdout + result.stderr
        with target.connect() as c:
            assert snapshot(c) == before and uids(c) == identity
    finally:
        source.dispose()
        if target is not None:
            target.dispose()
            _drop_db(target_name)
        _drop_db(source_name)
