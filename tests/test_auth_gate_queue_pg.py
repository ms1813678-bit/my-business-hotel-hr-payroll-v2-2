"""Actual authenticated HTTP request, middleware/auth/queued restore geometry."""
from concurrent.futures import ThreadPoolExecutor
import threading,time
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from app import main,dependencies
from app.db import get_db
from app.restore_lock import RESTORE_LOCK_KEY
from tests.test_login_concurrency_pg import pg_login,_prepare,_post,PASSWORD


@pytest.mark.parametrize('language,message',[('ar','النظام مشغول مؤقتًا، أعد المحاولة بعد قليل.'),('en','The system is temporarily busy. Try again shortly.'),('zh','系统暂时繁忙，请稍后重试。')])
def test_actual_http_second_auth_gate_refuses_queued_restore_without_cycle(pg_login,monkeypatch,language,message):
 factory,engine=pg_login
 def override_db():
  with factory() as db:yield db
 main.app.dependency_overrides[get_db]=override_db
 client=TestClient(main.app,raise_server_exceptions=False)
 try:
  client.cookies.set("my_hr_language",language)
  token=_prepare(client)
  response=_post(client,token,PASSWORD)
  assert response.status_code==303
  entered=threading.Event();resume=threading.Event();queued=threading.Event();restore_acquired=threading.Event();restore_release=threading.Event();pids=[]
  original=dependencies.acquire_authenticated_read_lock
  def pause_before_auth(db):
   entered.set();assert resume.wait(5)
   # Safety cap only: old blocking implementation fails as HTTP500 instead
   # of hanging the test indefinitely. Product fix uses no timeout change.
   db.execute(text("SET LOCAL lock_timeout='500ms'"))
   return original(db)
  monkeypatch.setattr(dependencies,'acquire_authenticated_read_lock',pause_before_auth)
  def restore():
   with engine.begin() as c:
    pids.append(c.scalar(text('SELECT pg_backend_pid()')));queued.set()
    c.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':RESTORE_LOCK_KEY})
    restore_acquired.set();assert restore_release.wait(5)
  with ThreadPoolExecutor(max_workers=2) as pool:
   request=pool.submit(client.get,'/employees')
   assert entered.wait(5)
   writer=pool.submit(restore);assert queued.wait(5)
   observed=False;until=time.monotonic()+3
   while time.monotonic()<until:
    with engine.connect() as c:observed=c.scalar(text("SELECT wait_event_type='Lock' AND wait_event='advisory' FROM pg_stat_activity WHERE pid=:p"),{'p':pids[0]}) is True
    if observed:break
    time.sleep(.01)
   try:
    assert observed
    resume.set();response=request.result(4)
    assert response.status_code==503,response.text
    assert response.headers['retry-after']=='5'
    assert response.json()=={'detail':message}
    assert restore_acquired.wait(3)
   finally:resume.set();restore_release.set()
   writer.result(4)
  # With the restore done the same authenticated cookie succeeds again.
  response=client.get('/employees');assert response.status_code==200
 finally:
  main.app.dependency_overrides.clear();client.close()


def test_existing_same_session_shared_gate_remains_reentrant(pg_login):
 from app.restore_lock import acquire_authenticated_read_lock
 factory,engine=pg_login
 with factory.begin() as db:
  acquire_authenticated_read_lock(db)
  acquire_authenticated_read_lock(db)
  assert db.scalar(text('SELECT 1'))==1


def test_exclusive_restore_owner_refuses_helper_without_auth_reads(pg_login):
 from app.restore_lock import acquire_authenticated_read_lock
 from fastapi import HTTPException
 factory,engine=pg_login
 with engine.begin() as exclusive:
  exclusive.execute(text('SELECT pg_advisory_xact_lock(:k)'),{'k':RESTORE_LOCK_KEY})
  with factory() as db:
   with pytest.raises(HTTPException) as err:acquire_authenticated_read_lock(db)
   assert err.value.status_code==503
   db.rollback()
