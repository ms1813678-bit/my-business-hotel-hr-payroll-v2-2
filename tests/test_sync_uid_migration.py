"""Migration 0067 (sync_uid) on real PostgreSQL, disposable schemas, full real chain.

Covers: legacy backfill with exact preservation, fresh/idempotent head, insert paths (raw SQL, Core,
explicit uid), DB-level rejection of bad/duplicate/changed/cleared uid (incl. bulk UPDATE), same-value
and unrelated edits, statement rollback, whole-migration failure rollback, populated-downgrade refusal,
clean-empty downgrade + re-upgrade. pg_dump/pg_restore is NOT covered here.
"""
import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.db import engine as configured_engine

ROOT = Path(__file__).resolve().parents[1]
PREV, REV = "0065_overtime_day_review", "0067_sync_uid_base_entities"
TABLES = ("departments", "employees", "leave_types", "leave_records")
HEAD = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
RX = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


@pytest.fixture()
def iso():
    if configured_engine.dialect.name != "postgresql":
        pytest.skip("Migrated PostgreSQL required")
    schema = "sync_uid_m_" + uuid.uuid4().hex[:12]
    with configured_engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    e = create_engine(configured_engine.url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield e
    finally:
        e.dispose()
        with configured_engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def mig(e, rev, down=False):
    cfg = Config(str(ROOT / "alembic.ini"))
    with e.begin() as c:
        cfg.attributes["connection"] = c
        (command.downgrade if down else command.upgrade)(cfg, rev)


def version(e):
    with e.connect() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar()


def seed(c):
    d = c.execute(text("INSERT INTO departments(name,active) VALUES ('Legacy',true) RETURNING id")).scalar_one()
    emp = c.execute(text("""INSERT INTO employees(code,full_name,department_id,position,hire_date,status,
        basic_salary,daily_hours,created_at) VALUES ('MY-9001','Legacy',:d,'p','2026-01-01','active',123.45,8,now())
        RETURNING id"""), {"d": d}).scalar_one()
    t = c.execute(text("""INSERT INTO leave_types(code,name_ar,name_en,is_paid,tracks_balance,active)
        VALUES ('legacy_t','x','x',true,true,true) RETURNING id""")).scalar_one()
    c.execute(text("""INSERT INTO leave_records(employee_id,leave_type_id,start_date,end_date,calendar_days,
        charged_days,reason,status,created_by,created_at) VALUES (:e,:t,'2026-01-01','2026-01-02',2,2,'r','draft','x',now())"""),
              {"e": emp, "t": t})


def snapshot(c, cols_minus=("sync_uid",)):
    out = {}
    for t in TABLES:
        rows = c.execute(text(f"SELECT to_jsonb(t) FROM {t} t ORDER BY id")).scalars().all()
        out[t] = [{k: v for k, v in r.items() if k not in cols_minus} for r in rows]
    return out


def uids(c):
    return {t: c.execute(text(f"SELECT id, sync_uid FROM {t} ORDER BY id")).all() for t in TABLES}


def test_legacy_rows_backfilled_exactly_and_head_is_idempotent(iso):
    mig(iso, PREV)
    with iso.begin() as c:
        seed(c)
        before = snapshot(c)
        seeded_types = c.execute(text("SELECT count(*) FROM leave_types")).scalar()
    mig(iso, REV)
    with iso.connect() as c:
        assert snapshot(c) == before                      # every non-UID column identical
        u = uids(c)
        import re
        for t, rows in u.items():
            vals = [r[1] for r in rows]
            assert len(vals) == len(set(vals)) and all(re.match(RX, v) for v in vals)
        assert len(u["leave_types"]) == seeded_types
    mig(iso, "head")
    with iso.connect() as c:
        assert uids(c) == u                               # re-upgrade keeps every uid
    assert version(iso) == HEAD


def test_fresh_install_has_objects_and_seeded_types_have_uids(iso):
    mig(iso, REV)
    with iso.connect() as c:
        assert c.execute(text("SELECT count(*) FROM leave_types WHERE sync_uid ~ :r"), {"r": RX}).scalar() >= 1
        trg = {r[0] for r in c.execute(text("SELECT g.tgname FROM pg_trigger g JOIN pg_class k ON k.oid=g.tgrelid WHERE k.relnamespace=current_schema()::regnamespace AND g.tgenabled='O' AND g.tgname LIKE 'trg_%_sync_uid_immutable'"))}
        assert trg == {f"trg_{t}_sync_uid_immutable" for t in TABLES}
        assert c.execute(text("SELECT count(*) FROM pg_trigger g JOIN pg_class k ON k.oid=g.tgrelid WHERE k.relnamespace=current_schema()::regnamespace AND g.tgname='trg_audit_logs_immutable'")).scalar() == 1
        for t in TABLES:
            col = c.execute(text("SELECT is_nullable, column_default FROM information_schema.columns "
                                 "WHERE table_name=:t AND column_name='sync_uid' AND table_schema=current_schema()"), {"t": t}).one()
            assert col[0] == "NO" and "sync_uid_new" in col[1]


def test_insert_paths_raw_core_explicit_and_db_rejections(iso):
    mig(iso, REV)
    good = str(uuid.uuid4())
    with iso.begin() as c:
        c.execute(text("INSERT INTO departments(name) VALUES ('raw1')"))                    # raw, column omitted
        c.execute(text("INSERT INTO departments(name,sync_uid) VALUES ('exp',:u)"), {"u": good})
        from sqlalchemy import Table, MetaData
        t = Table("departments", MetaData(), autoload_with=c)
        c.execute(t.insert().values(name="core1"))                                          # Core, omitted
        rows = c.execute(text("SELECT name, sync_uid FROM departments WHERE name IN ('raw1','exp','core1')")).all()
    import re
    assert len(rows) == 3 and all(re.match(RX, r[1]) for r in rows) and dict(rows)["exp"] == good
    assert len({r[1] for r in rows}) == 3
    bad_values = [None, "", "not-a-uuid", good.upper(), str(uuid.uuid1()), good.replace("-", ""), good + "0", " " + good]
    for i, bad in enumerate(bad_values):
        with pytest.raises(DBAPIError):
            with iso.begin() as c:
                c.execute(text("INSERT INTO departments(name,sync_uid) VALUES (:n,:u)"), {"n": f"b{i}", "u": bad})
    with pytest.raises(DBAPIError):                                                         # duplicate
        with iso.begin() as c:
            c.execute(text("INSERT INTO departments(name,sync_uid) VALUES ('dup',:u)"), {"u": good})
    with iso.connect() as c:
        assert c.execute(text("SELECT count(*) FROM departments")).scalar() == 3          # failed statements left no rows


@pytest.mark.parametrize("table,extra", [("departments", ""), ("employees", ""), ("leave_types", ""), ("leave_records", "")])
def test_uid_update_rules_at_db_layer_incl_bulk(iso, table, extra):
    mig(iso, PREV)
    with iso.begin() as c:
        seed(c)
    mig(iso, REV)
    with iso.connect() as c:
        before = uids(c)
    new = str(uuid.uuid4())
    for stmt in (f"UPDATE {table} SET sync_uid = :n",                                     # bulk change
                 f"UPDATE {table} SET sync_uid = NULL",
                 f"UPDATE {table} SET sync_uid = ''",
                 f"UPDATE {table} SET sync_uid = id::text"):
        with pytest.raises(DBAPIError):
            with iso.begin() as c:
                c.execute(text(stmt), {"n": new})
    with iso.connect() as c:
        assert uids(c) == before
    with iso.begin() as c:                                                                # allowed
        c.execute(text(f"UPDATE {table} SET sync_uid = sync_uid"))
    col = {"departments": "active", "employees": "position", "leave_types": "active", "leave_records": "reason"}[table]
    val = {"active": "false", "position": "'new'", "reason": "'new'"}[col]
    with iso.begin() as c:
        c.execute(text(f"UPDATE {table} SET {col} = {val}"))
    with iso.connect() as c:
        assert uids(c) == before


def test_whole_migration_failure_after_backfill_rolls_everything_back(iso):
    mig(iso, PREV)
    with iso.begin() as c:
        seed(c)
        # a relation with the name the migration wants for leave_records forces a late failure,
        # after departments/employees/leave_types were already altered and backfilled
        c.execute(text("CREATE TABLE uq_leave_records_sync_uid (x int)"))
    with pytest.raises(Exception):
        mig(iso, REV)
    assert version(iso) == PREV
    with iso.connect() as c:
        for t in TABLES:
            assert c.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name=:t "
                                  "AND column_name='sync_uid' AND table_schema=current_schema()"), {"t": t}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_proc WHERE pronamespace=current_schema()::regnamespace AND proname IN ('sync_uid_new','sync_uid_immutable')")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM employees")).scalar() == 1
    with iso.begin() as c:
        c.execute(text("DROP TABLE uq_leave_records_sync_uid"))
    mig(iso, REV)                                                                          # clean retry works
    assert version(iso) == REV


def test_populated_downgrade_refuses_without_touching_anything(iso):
    mig(iso, "head")                                  # seeded leave types already populate it
    with iso.connect() as c:
        before = uids(c)
    with pytest.raises(RuntimeError, match="Refusing to downgrade"):
        mig(iso, PREV, down=True)
    assert version(iso) == HEAD
    with iso.connect() as c:
        assert uids(c) == before
        assert c.execute(text("SELECT count(*) FROM pg_trigger g JOIN pg_class k ON k.oid=g.tgrelid WHERE k.relnamespace=current_schema()::regnamespace AND g.tgname LIKE 'trg_%_sync_uid_immutable'")).scalar() == 4


def test_clean_empty_downgrade_removes_only_candidate_objects_and_reupgrade_is_safe(iso):
    mig(iso, "head")
    with iso.begin() as c:                            # test-only emptying of an isolated disposable schema
        c.execute(text("DELETE FROM leave_types"))
    with iso.connect() as c:
        audit_before = c.execute(text("SELECT count(*) FROM pg_trigger g JOIN pg_class k ON k.oid=g.tgrelid WHERE k.relnamespace=current_schema()::regnamespace AND g.tgname='trg_audit_logs_immutable'")).scalar()
    mig(iso, PREV, down=True)
    assert version(iso) == PREV
    with iso.connect() as c:
        for t in TABLES:
            assert c.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name=:t "
                                  "AND column_name='sync_uid' AND table_schema=current_schema()"), {"t": t}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_proc WHERE pronamespace=current_schema()::regnamespace AND proname IN ('sync_uid_new','sync_uid_immutable')")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_trigger g JOIN pg_class k ON k.oid=g.tgrelid WHERE k.relnamespace=current_schema()::regnamespace AND g.tgname='trg_audit_logs_immutable'")).scalar() == audit_before == 1
    mig(iso, "head")
    assert version(iso) == HEAD
