"""Actual migrated-PG SELECT-only snapshot qualification, synthetic data."""
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from app.db import engine as configured
from app.models import Department, Employee, LeaveType, LeaveRecord
from app.sync_snapshot import read_uid_snapshot
from app.sync_uid import UnknownParent, UnknownEntity, MalformedShape, OutOfScopeRef
from app.sync_uid_mixin import SyncUidError

@pytest.fixture
def snapshot_db():
    if configured.dialect.name != 'postgresql':
        pytest.skip('Actual migrated PostgreSQL required')
    schema = 'sync_snapshot_' + uuid.uuid4().hex[:12]
    with configured.begin() as c:
        c.execute(text('CREATE SCHEMA ' + schema))
    engine = create_engine(configured.url, connect_args={'options': '-csearch_path=' + schema})
    try:
        cfg = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
        with engine.begin() as c:
            cfg.attributes['connection'] = c
            command.upgrade(cfg, 'head')
        with Session(engine, expire_on_commit=False) as db:
            d = Department(name='Synthetic snapshot')
            t = LeaveType(code='snapshot_type', name_ar='سنوي', name_en='Annual')
            db.add_all([d,t]); db.flush()
            e = Employee(code='MY-1701', full_name='Synthetic employee', department_id=d.id,
                         position='Reception', hire_date=date(2026,1,1), basic_salary=Decimal('1234.50'), daily_hours=Decimal('8'))
            db.add(e); db.flush()
            leave = LeaveRecord(employee_id=e.id, leave_type_id=t.id, start_date=date(2026,10,1), end_date=date(2026,10,2),
                                calendar_days=2, charged_days=Decimal('2'), reason='Synthetic', created_by='test')
            db.add(leave); db.commit()
            yield db, engine, (d,e,t,leave)
            db.rollback()
    finally:
        engine.dispose()
        with configured.begin() as c:
            c.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))


def test_actual_selected_values_parents_and_no_writes(snapshot_db):
    db,engine,(d,e,t,leave)=snapshot_db
    before={name:db.execute(text('SELECT row_to_json(x)::text FROM '+name+' x ORDER BY id')).scalars().all()
            for name in ['departments','employees','leave_types','leave_records','audit_logs']}
    db.expunge_all()
    statements=[]
    def record(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(engine,'before_cursor_execute',record)
    try:
        employee=read_uid_snapshot(db,'employees',e.sync_uid,['full_name','department_id','basic_salary','hire_date'])
        parent=read_uid_snapshot(db,'departments',d.sync_uid,['name','active'])
        kind=read_uid_snapshot(db,'leave_types',t.sync_uid,['name_en','is_paid'])
        rec=read_uid_snapshot(db,'leave_records',leave.sync_uid,['employee_id','leave_type_id','start_date','charged_days'])
    finally:
        event.remove(engine,'before_cursor_execute',record)
    assert len(statements)==4 and all(s.lstrip().upper().startswith('SELECT') for s in statements)
    assert not db.identity_map and not db.new and not db.dirty and not db.deleted
    assert employee.values=={'full_name':'Synthetic employee','department_uid':d.sync_uid,'basic_salary':Decimal('1234.50'),'hire_date':date(2026,1,1)}
    assert rec.values=={'employee_uid':e.sync_uid,'leave_type_uid':t.sync_uid,'start_date':date(2026,10,1),'charged_days':Decimal('2')}
    assert parent.values=={'name':'Synthetic snapshot','active':True}
    assert kind.values=={'name_en':'Annual','is_paid':True}
    assert not any(key in employee.values for key in ['id','national_id','phone','address','branch_id'])
    after={name:db.execute(text('SELECT row_to_json(x)::text FROM '+name+' x ORDER BY id')).scalars().all() for name in before}
    assert before==after
    assert not hasattr(employee,'version')

@pytest.mark.parametrize('fields',[None,[],['id'],['sync_uid'],['department'],['full_name','full_name'],['full_name',3],['unknown'], 'full_name'])
def test_explicit_field_shape_refuses_before_query(snapshot_db,fields):
    db,engine,(_,e,_,_)=snapshot_db
    statements=[]
    def record(*args):statements.append(args[2])
    event.listen(engine,'before_cursor_execute',record)
    try:
        with pytest.raises(MalformedShape):read_uid_snapshot(db,'employees',e.sync_uid,fields)
    finally:event.remove(engine,'before_cursor_execute',record)
    assert not statements

@pytest.mark.parametrize('mode',['new','dirty','deleted'])
def test_pending_writes_refuse_without_autoflush(snapshot_db,mode):
    db,engine,(d,e,_,_)=snapshot_db
    if mode=='new':db.add(Department(name='Must not flush'))
    elif mode=='dirty':e.full_name='Must not flush'
    else:db.delete(e)
    statements=[]
    def record(*args):statements.append(args[2])
    event.listen(engine,'before_cursor_execute',record)
    try:
        with pytest.raises(MalformedShape):read_uid_snapshot(db,'departments',d.sync_uid,['name'])
    finally:event.remove(engine,'before_cursor_execute',record)
    assert not statements
    db.rollback()
    assert db.get(Employee,e.id).full_name=='Synthetic employee'


def test_identity_and_unsupported_reference_refuse(snapshot_db):
    db,_,(d,e,_,_)=snapshot_db
    with pytest.raises(UnknownParent):read_uid_snapshot(db,'employees',d.sync_uid,['full_name'])
    with pytest.raises(UnknownParent):read_uid_snapshot(db,'employees',str(uuid.uuid4()),['full_name'])
    with pytest.raises(SyncUidError):read_uid_snapshot(db,'employees',e.id,['full_name'])
    with pytest.raises(UnknownEntity):read_uid_snapshot(db,'branches',e.sync_uid,['name'])
    with pytest.raises(OutOfScopeRef):read_uid_snapshot(db,'employees',e.sync_uid,['branch_id'])


def test_identical_integer_ids_in_another_schema_do_not_bind(snapshot_db):
    db,engine,(d,e,_,_)=snapshot_db
    other='snapshot_other_'+uuid.uuid4().hex[:12]
    foreign_uid=str(uuid.uuid4())
    with configured.begin() as c:
        c.execute(text('CREATE SCHEMA '+other))
        c.execute(text('CREATE TABLE '+other+'.employees (id integer, sync_uid varchar(36), full_name text)'))
        c.execute(text('INSERT INTO '+other+'.employees VALUES (:id,:uid,:name)'),{'id':e.id,'uid':foreign_uid,'name':'Other database employee'})
    try:
        with pytest.raises(UnknownParent):read_uid_snapshot(db,'employees',foreign_uid,['full_name'])
        assert read_uid_snapshot(db,'employees',e.sync_uid,['full_name']).values=={'full_name':'Synthetic employee'}
    finally:
        with configured.begin() as c:c.execute(text('DROP SCHEMA '+other+' CASCADE'))


def test_works_in_database_enforced_read_only_transaction(snapshot_db):
    db,_,(_,e,_,_)=snapshot_db
    uid=e.sync_uid
    db.rollback()
    db.execute(text('SET TRANSACTION READ ONLY'))
    assert db.execute(text('SHOW transaction_read_only')).scalar()=='on'
    assert read_uid_snapshot(db,'employees',uid,['full_name']).values=={'full_name':'Synthetic employee'}
    assert db.execute(text('SHOW transaction_read_only')).scalar()=='on'
