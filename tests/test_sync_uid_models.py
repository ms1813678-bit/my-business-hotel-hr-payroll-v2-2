"""Real-model tests for sync_uid (departments, employees, leave_types, leave_records).

These run on tables built by the REAL migration chain (alembic upgrade head) inside a disposable
PostgreSQL schema, so the NOT NULL / unique / CHECK / trigger objects are the migrated ones.
"""
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app import sync_uid as su
from app.db import engine as configured_engine
from app.models import Department, Employee, LeaveRecord, LeaveType
from app.sync_uid_mixin import SyncUidError, SyncUidImmutable

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db():
    if configured_engine.dialect.name != "postgresql":
        pytest.skip("Migrated PostgreSQL required")
    schema = "sync_uid_t_" + uuid.uuid4().hex[:12]
    with configured_engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    iso = create_engine(configured_engine.url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        with iso.begin() as c:
            cfg.attributes["connection"] = c
            command.upgrade(cfg, "head")
        with Session(iso) as s:
            yield s
            s.rollback()
    finally:
        iso.dispose()
        with configured_engine.begin() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def mk_dept(db, name="Front"):
    d = Department(name=name); db.add(d); db.flush(); return d


def mk_emp(db, d, code="MY-0001", **kw):
    e = Employee(code=code, full_name="A", department_id=d.id, position="p", hire_date=date(2024, 1, 1),
                 basic_salary=Decimal("1000"), daily_hours=Decimal("8"), **kw)
    db.add(e); db.flush(); return e


def mk_type(db, code="syn_uid_t"):
    t = LeaveType(code=code, name_ar="س", name_en="Annual"); db.add(t); db.flush(); return t


def mk_leave(db, e, t):
    r = LeaveRecord(employee_id=e.id, leave_type_id=t.id, start_date=date(2026, 1, 1),
                    end_date=date(2026, 1, 2), calendar_days=2, charged_days=Decimal("2"),
                    reason="r", created_by="hr")
    db.add(r); db.flush(); return r


def test_new_rows_get_canonical_uuid4_and_integer_ids_unchanged(db):
    d = mk_dept(db); e = mk_emp(db, d)
    for row in (d, e):
        assert uuid.UUID(row.sync_uid).version == 4 and str(uuid.UUID(row.sync_uid)) == row.sync_uid
    assert isinstance(d.id, int) and isinstance(e.id, int) and str(e.id) != e.sync_uid
    assert db.get(Employee, e.id) is e and e.department_id == d.id      # integer FK/PK path intact


def test_supplied_device_uid_is_kept_and_must_be_canonical(db):
    u = str(uuid.uuid4())
    d = Department(name="X", sync_uid=u); db.add(d); db.flush()
    assert d.sync_uid == u
    for bad in ("not-a-uuid", u.upper(), str(uuid.uuid1()), u.replace("-", ""), 5):
        with pytest.raises(SyncUidError):
            db.add(Department(name="B" + str(bad)[:4], sync_uid=bad)); db.flush()
        db.rollback()


def test_uid_unique_per_table(db):
    u = str(uuid.uuid4())
    db.add(Department(name="A", sync_uid=u)); db.flush()
    db.add(Department(name="B", sync_uid=u))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_uid_is_write_once_even_when_unloaded(db):
    d = mk_dept(db); old = d.sync_uid; db.commit()
    d.sync_uid = str(uuid.uuid4())
    with pytest.raises(SyncUidImmutable):
        db.flush()
    db.rollback()
    d = db.get(Department, d.id); assert d.sync_uid == old
    db.expire(d, ["sync_uid"])                       # attribute not loaded when set
    d.sync_uid = str(uuid.uuid4())
    with pytest.raises(SyncUidImmutable):
        db.flush()
    db.rollback()
    d = db.get(Department, d.id); d.sync_uid = None
    with pytest.raises(SyncUidImmutable):
        db.flush()
    db.rollback()
    d = db.get(Department, d.id); d.sync_uid = old; d.active = False; db.flush()   # same value is fine
    assert db.get(Department, d.id).sync_uid == old


def test_refs_roundtrip_for_leave_record_and_employee(db):
    d = mk_dept(db); e = mk_emp(db, d); t = mk_type(db); r = mk_leave(db, e, t)
    wire = su.refs_to_uids(db, "leave_records", {"employee_id": e.id, "leave_type_id": t.id, "reason": "r"})
    assert wire == {"employee_uid": e.sync_uid, "leave_type_uid": t.sync_uid, "reason": "r"}
    assert su.uids_to_refs(db, "leave_records", wire) == {"employee_id": e.id, "leave_type_id": t.id, "reason": "r"}
    w2 = su.refs_to_uids(db, "employees", {"department_id": d.id})
    assert su.uids_to_refs(db, "employees", w2) == {"department_id": d.id}
    assert su.resolve(db, "leave_records", r.sync_uid) is r


def test_unknown_or_wrong_entity_uid_fails_closed(db):
    d = mk_dept(db); e = mk_emp(db, d); t = mk_type(db)
    with pytest.raises(su.UnknownParent):
        su.uids_to_refs(db, "leave_records", {"employee_uid": str(uuid.uuid4()), "leave_type_uid": t.sync_uid})
    with pytest.raises(su.UnknownParent):        # an employee uid is not a leave_type uid
        su.uids_to_refs(db, "leave_records", {"employee_uid": e.sync_uid, "leave_type_uid": e.sync_uid})
    assert su.resolve(db, "departments", e.sync_uid) is None
    with pytest.raises(su.UnknownEntity):
        su.resolve(db, "payroll_items", e.sync_uid)
    with pytest.raises(SyncUidError):
        su.resolve(db, "employees", "123")


def test_out_of_scope_branch_fails_closed_and_ensure_uid_is_noop_when_present(db):
    d = mk_dept(db); e = mk_emp(db, d)
    assert su.refs_to_uids(db, "employees", {"department_id": d.id, "branch_id": None})["branch_id"] is None
    assert su.ensure_uid(db, d) == d.sync_uid
    with pytest.raises(su.OutOfScopeRef):
        su.refs_to_uids(db, "employees", {"department_id": d.id, "branch_id": 3})
    with pytest.raises(su.OutOfScopeRef):
        su.uids_to_refs(db, "employees", {"department_uid": str(uuid.uuid4()), "branch_id": 3})


def test_metadata_adds_sync_uid_to_exactly_the_four_scoped_tables():
    from app.db import Base
    with_uid = {t.name for t in Base.metadata.sorted_tables if "sync_uid" in t.c}
    assert with_uid == {"departments", "employees", "leave_types", "leave_records"}
    for name in with_uid:
        c = Base.metadata.tables[name].c.sync_uid
        t = Base.metadata.tables[name]
        assert not c.nullable and c.type.length == 36 and not c.primary_key
        names = {k.name for k in t.constraints}
        assert {f"uq_{name}_sync_uid", f"ck_{name}_sync_uid_v4"} <= names


def test_no_other_model_column_changed():
    """Integer PK/FK columns of the four tables keep their types."""
    for model in (Department, Employee, LeaveType, LeaveRecord):
        assert model.__table__.c.id.primary_key and str(model.__table__.c.id.type) == "INTEGER"
    assert {fk.target_fullname for fk in LeaveRecord.__table__.foreign_keys} == {"employees.id", "leave_types.id"}


def test_wire_mapping_rejects_raw_keys_and_both_forms(db):
    d = mk_dept(db); e = mk_emp(db, d); t = mk_type(db)
    base = {"employee_uid": e.sync_uid, "leave_type_uid": t.sync_uid}
    for extra in ({"employee_id": e.id}, {"leave_type_id": 1}, {"id": 7}, {"sync_uid": e.sync_uid}):
        with pytest.raises(su.MalformedShape):
            su.uids_to_refs(db, "leave_records", {**base, **extra})
    with pytest.raises(su.MalformedShape):                    # raw FK without uid
        su.uids_to_refs(db, "leave_records", {"employee_id": e.id, "leave_type_uid": t.sync_uid})
    with pytest.raises(su.MalformedShape):
        su.uids_to_refs(db, "employees", {"department_id": d.id, "department_uid": d.sync_uid})
    for bad in (None, 5, b"x", ["u"]):
        with pytest.raises(su.MalformedShape):
            su.uids_to_refs(db, "employees", {"department_uid": bad})


def test_db_form_mapping_rejects_ambiguity_bad_ids_and_bad_shapes(db):
    d = mk_dept(db)
    with pytest.raises(su.MalformedShape):
        su.refs_to_uids(db, "employees", {"department_id": d.id, "department_uid": d.sync_uid})
    with pytest.raises(su.MalformedShape):
        su.refs_to_uids(db, "employees", {"id": 1, "department_id": d.id})
    for bad in (True, False, 0, -1, "1", 1.0, None):
        with pytest.raises(su.MalformedShape):
            su.refs_to_uids(db, "employees", {"department_id": bad})
    for shape in (None, [], "x", {1: 2}):
        with pytest.raises(su.MalformedShape):
            su.refs_to_uids(db, "employees", shape)
        with pytest.raises(su.MalformedShape):
            su.uids_to_refs(db, "employees", shape)
    with pytest.raises(su.MalformedShape):
        su.uid_of(db, "departments", True)


def test_uid_namespace_is_per_table_same_uid_may_exist_in_two_tables(db):
    u = str(uuid.uuid4())
    d = Department(name="Z", sync_uid=u); t = LeaveType(code="syn_uid_c", name_ar="a", name_en="e", sync_uid=u)
    db.add_all([d, t]); db.flush()
    assert su.resolve(db, "departments", u) is d and su.resolve(db, "leave_types", u) is t
