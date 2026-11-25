"""Actual FastAPI dependency/CSRF routes with migrated PG and signed live session.

Uses a dedicated test app mounting the unregistered router, not product login.
The real get_current_admin resolves the signed live DB session on every request.
"""
import base64,json,re
from decimal import Decimal
import pytest
from itsdangerous import TimestampSigner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select,func
from app.db import get_db
from app.models import AdminAccount,OvertimeDayAward,OvertimeDayReviewReceipt
from tests.test_overtime_day_concurrency import committed_day  # noqa:F401

@pytest.fixture
def daily_client(committed_day,monkeypatch):
    from app.main import templates
    from app.overtime_day_routes import overtime_day_router
    Session,auth,item_id,attendance_id=committed_day
    monkeypatch.setattr('app.overtime_day_routes.SessionLocal',Session)
    application=FastAPI();application.add_middleware(SessionMiddleware,secret_key='synthetic-router-session-only')
    application.include_router(overtime_day_router(templates))
    def db():
        with Session() as session:yield session
    application.dependency_overrides[get_db]=db
    body={'admin_username':'synthetic-ot-race','admin_session_version':auth['session_version'],'admin_session_id':auth['session_id'],'_csrf_token':'synthetic-csrf-only'}
    signed=TimestampSigner('synthetic-router-session-only').sign(base64.b64encode(json.dumps(body).encode())).decode()
    with TestClient(application) as client:
        client.cookies.set('session',signed)
        yield client,Session,auth,item_id


def form():return dict(csrf_token='synthetic-csrf-only',work_date='2026-02-02',manual_hours='3',rate_divisor='9',reason='Explicit excess reason',predecessor_id='')

@pytest.mark.parametrize('role',['system_admin','finance','assistant_manager'])
def test_actual_role_csrf_review_confirm_replace(daily_client,role):
    client,Session,auth,item_id=daily_client;url=f'/payroll/items/{item_id}/overtime-days'
    with Session.begin() as db:db.get(AdminAccount,auth['actor_id']).role=role
    page=client.get(url)
    if role=='assistant_manager':
        assert page.status_code==403
        assert client.post(url+'/review',data=form()).status_code==403
        return
    assert page.status_code==200 and page.headers['cache-control']=='no-store'
    bad=form();bad.pop('csrf_token')
    assert client.post(url+'/review',data=bad).status_code==403
    capture_form=form()
    import os
    if os.environ.get('OT_VISUAL_CAPTURE') and role=='system_admin':
        from app.models import PayrollItem
        with Session.begin() as db:
            db.get(PayrollItem,item_id).employee_name_snapshot='أحمد محمود، موظف تجريبي'
            db.get(PayrollItem,item_id).employee_code_snapshot='تجريبي ١٠١'
        capture_form['reason']='تكليف إضافي مسجل من مسؤول القسم بعد انتهاء الوردية'
    reviewed=client.post(url+'/review',data=capture_form);assert reviewed.status_code==200
    match=re.search(r'name="receipt_id" value="([a-f0-9]{64})"',reviewed.text);assert match
    receipt=match.group(1)
    import os
    if os.environ.get('OT_VISUAL_CAPTURE') and role=='system_admin':
        from pathlib import Path
        Path('/downloads/item2-daily-review1150.html').write_text(reviewed.text.replace('href="/static/','href="file:///downloads/recovered-ui/app/static/').replace('src="/static/','src="file:///downloads/recovered-ui/app/static/'),encoding='utf-8')
    with Session() as db:assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==0
    confirmed=client.post(url+'/confirm',data=dict(csrf_token='synthetic-csrf-only',receipt_id=receipt,human_confirmed='yes'));assert confirmed.status_code==200
    assert '3.000000' in confirmed.text
    fixed=form()|dict(predecessor_id=receipt,manual_hours='2',reason='Correction reason')
    reviewed=client.post(url+'/review',data=fixed);next_id=re.search(r'name="receipt_id" value="([a-f0-9]{64})"',reviewed.text).group(1)
    assert client.post(url+'/confirm',data=dict(csrf_token='synthetic-csrf-only',receipt_id=next_id,human_confirmed='yes')).status_code==200
    with Session() as db:assert db.scalar(select(func.count()).select_from(OvertimeDayAward))==2


def test_duplicate_form_and_session_revocation(daily_client):
    client,Session,auth,item_id=daily_client;url=f'/payroll/items/{item_id}/overtime-days'
    data=form();data['manual_hours']='0.0000001'
    assert client.post(url+'/review',data=data,follow_redirects=False).status_code==303
    with Session() as db:assert db.scalar(select(func.count()).select_from(OvertimeDayReviewReceipt))==0
    from app.models import AdminSession
    with Session.begin() as db:db.delete(db.get(AdminSession,auth['session_id']))
    assert client.get(url).status_code==401



def test_monthly_route_explicit_review_and_source_digest(daily_client):
    from app.models import PayrollItem
    client,Session,auth,item_id=daily_client;url=f'/payroll/items/{item_id}/overtime-days'
    reviewed=client.post(url+'/review',data=form());receipt=re.search(r'name="receipt_id" value="([a-f0-9]{64})"',reviewed.text).group(1)
    client.post(url+'/confirm',data=dict(csrf_token='synthetic-csrf-only',receipt_id=receipt,human_confirmed='yes'))
    page=client.get(url);digest=re.search(r'name="expected_digest" value="([a-f0-9]{64})"',page.text).group(1)
    with Session() as db:assert db.get(PayrollItem,item_id).overtime_amount==0
    response=client.post(url+'/monthly',data=dict(csrf_token='synthetic-csrf-only',expected_digest=digest,human_confirmed='yes'),follow_redirects=False)
    assert response.status_code==303
    with Session() as db:
        item=db.get(PayrollItem,item_id);assert item.overtime_amount==Decimal('66.67') and item.overtime_daily_digest==digest


def test_product_app_daily_review_monthly_and_legacy_bypass(committed_day,monkeypatch):
    from app.main import app
    from app.config import settings
    from app.models import PayrollRun,PayrollItem,AttendanceMonth
    Session,auth,item_id,attendance_id=committed_day
    monkeypatch.setattr('app.overtime_day_routes.SessionLocal',Session);monkeypatch.setattr('app.main.SessionLocal',Session)
    def db():
        with Session() as session:yield session
    app.dependency_overrides[get_db]=db
    with Session.begin() as session:
        item=session.get(PayrollItem,item_id);run=session.get(PayrollRun,item.payroll_run_id);run.overtime_daily_required=True;run_id=run.id
        session.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'))
    body={'admin_username':'synthetic-ot-race','admin_session_version':auth['session_version'],'admin_session_id':auth['session_id'],'_csrf_token':'synthetic-csrf-only'}
    signed=TimestampSigner(settings.session_secret).sign(base64.b64encode(json.dumps(body).encode())).decode()
    try:
        with TestClient(app) as client:
            client.cookies.set('session',signed)
            # A monthly-only forged update and legacy approve cannot award money.
            bad=client.post(f'/payroll/items/{item_id}/update',data=dict(csrf_token='synthetic-csrf-only',overtime_hours='9'),follow_redirects=False);assert bad.status_code==303
            client.post(f'/payroll/items/{item_id}/overtime-approve',data=dict(csrf_token='synthetic-csrf-only'),follow_redirects=False)
            with Session() as session:assert session.get(PayrollItem,item_id).overtime_amount==0 and session.get(PayrollItem,item_id).overtime_hours==0
            url=f'/payroll/items/{item_id}/overtime-days';reviewed=client.post(url+'/review',data=form())
            assert reviewed.status_code==200
            receipt=re.search(r'name="receipt_id" value="([a-f0-9]{64})"',reviewed.text).group(1)
            client.post(url+'/confirm',data=dict(csrf_token='synthetic-csrf-only',receipt_id=receipt,human_confirmed='yes'))
            page=client.get(url);digest=re.search(r'name="expected_digest" value="([a-f0-9]{64})"',page.text).group(1)
            client.post(url+'/monthly',data=dict(csrf_token='synthetic-csrf-only',expected_digest=digest,human_confirmed='yes'),follow_redirects=False)
            approved=client.post(f'/payroll/runs/{run_id}/approve',data=dict(csrf_token='synthetic-csrf-only'),follow_redirects=False);assert 'error=' not in approved.headers['location']
            closed=client.post(f'/payroll/runs/{run_id}/close',data=dict(csrf_token='synthetic-csrf-only'),follow_redirects=False);assert 'error=' not in closed.headers['location']
            paid=client.post(f'/payroll/items/{item_id}/payments',data=dict(csrf_token='synthetic-csrf-only',amount='1',paid_date='2026-03-05',method='cash',reference='Synthetic route lifecycle'),follow_redirects=False);assert 'error=' not in paid.headers['location']
            with Session() as session:assert session.get(PayrollItem,item_id).overtime_amount==Decimal('66.67') and session.get(PayrollRun,run_id).status=='closed'
    finally:app.dependency_overrides.clear()

@pytest.mark.parametrize('change',['epoch','session','role'])
def test_between_auth_and_service_changes_refuse(committed_day,monkeypatch,change):
    from app.main import app
    from app.config import settings
    from app.models import AdminSession,SystemSetting
    import app.overtime_day_routes as routes
    Session,auth,item_id,attendance_id=committed_day
    monkeypatch.setattr(routes,'SessionLocal',Session)
    original=routes.prepare_day_award
    def changed(db,**kwargs):
        with Session.begin() as other:
            if change=='epoch':other.merge(SystemSetting(key='restore_epoch',value='synthetic-changed-epoch'))
            elif change=='session':other.delete(other.get(AdminSession,auth['session_id']))
            else:other.get(AdminAccount,auth['actor_id']).role='assistant_manager'
        return original(db,**kwargs)
    monkeypatch.setattr(routes,'prepare_day_award',changed)
    def db():
        with Session() as session:yield session
    app.dependency_overrides[get_db]=db
    body={'admin_username':'synthetic-ot-race','admin_session_version':auth['session_version'],'admin_session_id':auth['session_id'],'_csrf_token':'synthetic-csrf-only'}
    signed=TimestampSigner(settings.session_secret).sign(base64.b64encode(json.dumps(body).encode())).decode()
    try:
        with TestClient(app) as client:
            client.cookies.set('session',signed)
            result=client.post(f'/payroll/items/{item_id}/overtime-days/review',data=form(),follow_redirects=False)
            assert result.status_code==303 and 'error=' in result.headers['location']
        with Session() as session:
            assert session.scalar(select(func.count()).select_from(OvertimeDayReviewReceipt))==0
    finally:app.dependency_overrides.clear()


def test_daily_template_english_chinese_have_no_untranslated_ui():
    from app.main import templates
    from app.i18n import translate_html
    from types import SimpleNamespace as NS
    from datetime import date,datetime,UTC
    from starlette.requests import Request
    request=Request({'type':'http','method':'GET','path':'/','headers':[],'query_string':b'','session':{},'state':{'language':'ar','admin_role':'system_admin'}})
    item=NS(id=1,employee_name_snapshot='Test Employee',employee_code_snapshot='TEST',salary_basis=6000,performance_bonus=1000)
    run=NS(id=1,period_start=date(2026,2,1),period_end=date(2026,2,28),status='draft')
    review=dict(id='a'*64,proposal=dict(work_date='2026-02-02',manual_hours='3',rate_divisor=9,reason='User-entered reason',predecessor_id=None),extra=2,amount=Decimal('77.777'),expires=datetime.now(UTC))
    # Filter registration is part of the actual router factory.
    from app.overtime_day_routes import overtime_day_router
    overtime_day_router(templates)
    rendered=templates.env.get_template('overtime_day_review.html').render(request=request,item=item,run=run,review=review,awards=[],monthly=None,csrf_token='synthetic',admin='synthetic')
    for language in ('en','zh'):
        out=translate_html(rendered,language)
        assert not re.search('[\u0600-\u06ff]',out),re.findall('[\u0600-\u06ff][^<]*',out)
    review['proposal']['reason']=''
    rendered=templates.env.get_template('overtime_day_review.html').render(request=request,item=item,run=run,review=review,awards=[],monthly=None,csrf_token='synthetic',admin='synthetic')
    for language in ('en','zh'):assert not re.search('[\u0600-\u06ff]',translate_html(rendered,language))


@pytest.mark.parametrize('competitor',['login','daily_post','restore_gate'])
def test_product_get_shared_read_vs_login_post_restore(committed_day,monkeypatch,competitor):
    import threading
    from sqlalchemy import text
    from app.main import app
    from app.config import settings
    from app.security import hash_password
    from app.restore_lock import RESTORE_LOCK_KEY
    import app.overtime_day_routes as routes
    Session,auth,item_id,attendance_id=committed_day
    monkeypatch.setattr(routes,'SessionLocal',Session);monkeypatch.setattr('app.main.SessionLocal',Session)
    def db():
        with Session() as session:yield session
    app.dependency_overrides[get_db]=db
    with Session.begin() as session:session.get(AdminAccount,auth['actor_id']).password_hash=hash_password('Synthetic-Get-Gate-123!')
    body={'admin_username':'synthetic-ot-race','admin_session_version':auth['session_version'],'admin_session_id':auth['session_id'],'_csrf_token':'synthetic-csrf-only'}
    signed=TimestampSigner(settings.session_secret).sign(base64.b64encode(json.dumps(body).encode())).decode()
    entered=threading.Event();release=threading.Event();started=threading.Event();done=threading.Event();errors=[];out={}
    original=routes.authority
    def pause(db,auth,**kwargs):
        result=original(db,auth,**kwargs)
        if kwargs.get('read_only'):
            # Read-only shared gate must coexist with a second reader now.
            with Session.begin() as second:
                second.execute(text("SET LOCAL lock_timeout='1000ms'"))
                second.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'),dict(key=RESTORE_LOCK_KEY))
            entered.set();assert release.wait(8)
        return result
    monkeypatch.setattr(routes,'authority',pause)
    def get():
        try:
            with TestClient(app) as client:
                client.cookies.set('session',signed);out['get']=client.get(f'/payroll/items/{item_id}/overtime-days').status_code
        except Exception as exc:errors.append(exc)
    def competing():
        try:
            started.set()
            if competitor=='restore_gate':
                with Session.begin() as session:
                    session.execute(text('SELECT pg_advisory_xact_lock(:key)'),dict(key=RESTORE_LOCK_KEY));out['other']=200
            else:
                with TestClient(app) as client:
                    client.cookies.set('session',signed)
                    if competitor=='login':result=client.post('/login',data=dict(csrf_token='synthetic-csrf-only',username='synthetic-ot-race',password='Synthetic-Get-Gate-123!'),follow_redirects=False)
                    else:result=client.post(f'/payroll/items/{item_id}/overtime-days/review',data=form(),follow_redirects=False)
                    out['other']=result.status_code
            done.set()
        except Exception as exc:errors.append(exc)
    reader=threading.Thread(target=get);writer=threading.Thread(target=competing)
    try:
        reader.start();assert entered.wait(5)
        writer.start();assert started.wait(3);assert not done.wait(.15)
    finally:
        release.set();reader.join(10)
        if writer.ident is not None:writer.join(10)
        app.dependency_overrides.clear()
    assert not reader.is_alive() and not writer.is_alive() and not errors,errors
    assert out['get']==200
    assert out['other']==(303 if competitor=='login' else 200)


def test_daily_get_must_keep_middleware_gate():
    from app.restore_lock import restore_gate_exempt
    assert not restore_gate_exempt('GET','/payroll/items/1/overtime-days')
