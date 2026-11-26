"""Actual HTTP/CSRF login counter serialization through the existing restore gate."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
import json,re,threading,time,uuid
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select,text,func
from sqlalchemy.orm import sessionmaker
from app import main
from app.db import engine as configured
from app.models import AdminAccount,AuditLog,SystemSetting,AdminSession
from app.security import hash_password
from app.login_security import LOCKOUT_DURATION,login_key,lockout_remaining

PASSWORD='Synthetic-Login-Qualification-1698!'

@pytest.fixture
def pg_login(monkeypatch):
    if configured.dialect.name!='postgresql':pytest.skip('Actual migrated PostgreSQL required')
    schema='login_http_'+uuid.uuid4().hex[:12]
    with configured.begin() as c:c.execute(text('CREATE SCHEMA '+schema))
    engine=create_engine(configured.url,connect_args={'options':'-csearch_path='+schema})
    try:
        cfg=Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
        with engine.begin() as c:cfg.attributes['connection']=c;command.upgrade(cfg,'head')
        factory=sessionmaker(engine,autoflush=False)
        with factory.begin() as db:db.add(AdminAccount(username='synthetic-admin',password_hash=hash_password(PASSWORD),active=True,role='system_admin'))
        monkeypatch.setattr(main,'SessionLocal',factory)
        monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:True)
        monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:True)
        yield factory,engine
    finally:
        engine.dispose()
        with configured.begin() as c:c.execute(text('DROP SCHEMA '+schema+' CASCADE'))

def _prepare(client):
    page=client.get('/login');assert page.status_code==200
    return re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)

def _post(client,token,password='Wrong-synthetic-password-1698!'):
    return client.post('/login',data={'username':'synthetic-admin','password':password,'csrf_token':token},follow_redirects=False)

@pytest.mark.parametrize('existing_failures',[1,2])
def test_actual_http_failures_serialize_before_auth_and_lock_once(pg_login,monkeypatch,existing_failures):
    factory,engine=pg_login
    if existing_failures:
        with factory.begin() as db:db.add(SystemSetting(key=login_key('synthetic-admin'),value=json.dumps({'failed_attempts':existing_failures,'locked_until':None}),updated_by='test'))
    entered=threading.Event();release=threading.Event();second_gate=threading.Event();pids=[];guard=threading.Lock();calls=[]
    original_auth=main.authenticate;original_gate=main.acquire_credential_write_lock
    def gate(db,*args,**kwargs):
        pid=db.scalar(text('SELECT pg_backend_pid()'))
        with guard:
            pids.append(pid)
            if len(pids)==2:second_gate.set()
        return original_gate(db,*args,**kwargs)
    def auth(db,*args,**kwargs):
        with guard:calls.append(1);first=len(calls)==1
        if first:
            entered.set();assert release.wait(10),'first login release timed out'
        return original_auth(db,*args,**kwargs)
    monkeypatch.setattr(main,'acquire_credential_write_lock',gate);monkeypatch.setattr(main,'authenticate',auth)
    one,two=TestClient(main.app),TestClient(main.app)
    try:
        tokens=[_prepare(one),_prepare(two)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(_post,one,tokens[0]);assert entered.wait(10)
            b=pool.submit(_post,two,tokens[1]);assert second_gate.wait(10)
            observed=False
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    with configured.connect() as c:
                        observed=c.execute(text("SELECT wait_event_type='Lock' AND wait_event='advisory' FROM pg_stat_activity WHERE pid=:pid"),{'pid':pids[1]}).scalar() is True
                    if observed:break
                    time.sleep(.02)
                assert observed,'second login never observed waiting on actual advisory lock'
                assert len(calls)==1 and not b.done()
            finally:release.set()
            responses=[a.result(timeout=10),b.result(timeout=10)]
        assert [r.status_code for r in responses]==([401,429] if existing_failures==1 else [429,429])
        assert len(calls)==(2 if existing_failures==1 else 1)
        with factory() as db:
            state=json.loads(db.get(SystemSetting,login_key('synthetic-admin')).value)
            assert state['failed_attempts']==3
            assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action=='login_lockout'))==1
            assert db.scalar(select(func.count(AdminSession.id)))==0
            until=datetime.fromisoformat(state['locked_until'])
            assert timedelta(minutes=59)<until-datetime.now(timezone.utc)<=LOCKOUT_DURATION
            # New sessions observe persisted lockout, not a Python-process counter.
            assert lockout_remaining(db,'synthetic-admin') is not None
        blocked=_post(one,tokens[0],PASSWORD)
        assert blocked.status_code==429 and '60' in blocked.text
        with factory() as db:
            assert json.loads(db.get(SystemSetting,login_key('synthetic-admin')).value)==state
            assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action=='login_lockout'))==1
    finally:release.set();one.close();two.close()
