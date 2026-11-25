"""Actual app HTTP, login/session/CSRF/middleware. Disposable DB only."""
import re
from datetime import date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from app import main
from app.db import SessionLocal
from app.models import AdminAccount, Department, Employee
from app.security import hash_password
from app.restore_lock import read_restore_epoch
from tests.test_leave_opening_apply import confirmed
from types import SimpleNamespace


def token(r):
    assert r.status_code==200
    return re.search(r'name="csrf_token" value="([^"]+)"',r.text).group(1)


@pytest.mark.parametrize('lang',['ar','en','zh'])
def test_actual_http_review_apply_csrf_role_replay(lang,monkeypatch):
    assert not main.app.dependency_overrides
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    with SessionLocal.begin() as db:
        d=Department(name='Synthetic HTTP '+lang);db.add(d);db.flush()
        e=Employee(code='HTTP-'+lang,full_name='Synthetic HTTP',department_id=d.id,position='synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8));db.add(e);db.flush();eid=e.id
        for role in ('system_admin','finance','assistant_manager'):
            db.add(AdminAccount(username='http-'+lang+'-'+role,password_hash=hash_password('Synthetic-Only-HTTP-1712!'),role=role,active=True,must_change_password=False))
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',lang)
        r=client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'http-'+lang+'-system_admin','password':'Synthetic-Only-HTTP-1712!'},follow_redirects=False);assert r.status_code==303
        # Source setup uses the actual signed session from this login, not a fake identity override.
        import base64,json
        raw=client.cookies.get('session').split('.')[0]
        session=json.loads(base64.b64decode(raw+'='*((4-len(raw)%4)%4)))
        with SessionLocal.begin() as db:
            a=db.scalar(select(AdminAccount).where(AdminAccount.username=='http-'+lang+'-system_admin'))
            auth=dict(actor_id=a.id,session_id=session['admin_session_id'],session_version=session['admin_session_version'],expected_epoch=read_restore_epoch(db))
            source=confirmed(db,SimpleNamespace(employee_id=eid),auth)
            data=dict(group_id=source.group_id,policy_id=source.policy_id,selection_id=source.id,reason='Synthetic HTTP',operation_stage='review_apply')
        old_data=dict(data)
        with SessionLocal.begin() as db:
            replacement=confirmed(db,SimpleNamespace(employee_id=eid),auth,a='8.250000000000000000000001',w='3',h='0')
            data.update(group_id=replacement.group_id,policy_id=replacement.policy_id,selection_id=replacement.id)
        csrf=token(client.get('/leave/opening'))
        assert client.post('/leave/opening/confirm-source',data={**old_data,'csrf_token':csrf},follow_redirects=False).headers['location']=='/leave/opening?error=1'
        assert client.post('/leave/opening/confirm-source',data=data).status_code==403
        preview=client.post('/leave/opening/confirm-source',data={**data,'csrf_token':csrf});assert preview.status_code==200,preview.text
        digest=preview.json()['apply_sha256']
        saved_url='/leave/opening/groups/'+data['group_id']+'?policy_id='+data['policy_id']
        page=client.get(saved_url);assert page.status_code==200,page.text
        assert 'name="reviewed_apply_sha256"' in page.text
        if lang != 'ar':
            assert not re.search(r'[\u0600-\u06ff]',page.text.split('<main class="content">')[1].split('<footer>')[0])

        apply={**data,'operation_stage':'apply','reviewed_apply_sha256':digest,'human_confirmed':'confirmed','csrf_token':csrf}
        bad=client.post('/leave/opening/confirm-source',data={**apply,'human_confirmed':''},follow_redirects=False);assert bad.status_code==303
        for altered in ({**apply,'reviewed_apply_sha256':'a'*64},{**apply,'selection_id':'b'*64},
                        {**apply,'unexpected':'extra'}):
            assert client.post('/leave/opening/confirm-source',data=altered,follow_redirects=False).headers['location']=='/leave/opening?error=1'
        from urllib.parse import urlencode
        doubled=urlencode(apply)+'&selection_id='+apply['selection_id']
        assert client.post('/leave/opening/confirm-source',content=doubled,headers={'content-type':'application/x-www-form-urlencoded'},follow_redirects=False).headers['location']=='/leave/opening?error=1'
        if lang == 'ar':
            from concurrent.futures import ThreadPoolExecutor
            from time import monotonic,sleep
            with SessionLocal() as blocker:
                blocker.execute(text('LOCK TABLE leave_types IN ROW EXCLUSIVE MODE'))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending=pool.submit(client.post,'/leave/opening/confirm-source',data=apply)
                    limit=monotonic()+3
                    waiting=False
                    while monotonic()<limit:
                        with SessionLocal() as observer:
                            waiting=observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'LOCK TABLE audit_logs%')"))
                        if waiting:break
                        sleep(.01)
                    assert waiting,'Actual HTTP apply never observed waiting for table lock'
                    start=monotonic()
                    static=client.get('/static/hotel-logo-transparent.png')
                    assert static.status_code==200 and monotonic()-start<1.0
                    assert not pending.done()
                    blocker.rollback()
                    out=pending.result(timeout=5)
        else:
            out=client.post('/leave/opening/confirm-source',data=apply)
        assert out.status_code==200,out.text
        assert not out.json()['replayed'] and len(out.json()['ledger_ids'])==2
        assert client.post('/leave/opening/confirm-source',data=apply).json()['replayed']
        html=client.post('/leave/opening/confirm-source',data={**apply,'response_format':'html'})
        assert html.status_code==200 and 'name="reviewed_apply_sha256"' not in html.text
        assert 'action="/leave/opening/confirm-source"' not in html.text
        if lang != 'ar':
            assert not re.search(r'[\u0600-\u06ff]',html.text.split('<main class="content">')[1].split('<footer>')[0])

        for role in ('finance','assistant_manager'):
            client.cookies.clear()
            assert client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'http-'+lang+'-'+role,'password':'Synthetic-Only-HTTP-1712!'},follow_redirects=False).status_code==303
            denied_csrf=token(client.get('/login'))
            assert client.post('/leave/opening/confirm-source',data={**apply,'csrf_token':denied_csrf}).status_code==403
    with SessionLocal() as db:
        assert db.scalar(text('SELECT count(*) FROM leave_balance_transactions WHERE employee_id=:e'),dict(e=eid))==2


def test_full_ui_entry_source_review_confirm_and_opening_apply(monkeypatch):
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    with SessionLocal.begin() as db:
        d=Department(name='Synthetic full opening');db.add(d);db.flush()
        e=Employee(code='FULL-OPENING',full_name='Synthetic opening',department_id=d.id,position='synthetic',hire_date=date(2025,1,1),status='active',basic_salary=1,daily_hours=8)
        db.add(e);db.flush();eid=e.id
        db.add(AdminAccount(username='synthetic-full-opening',password_hash=hash_password('Synthetic-Only-HTTP-1712!'),role='system_admin',active=True,must_change_password=False))
    with TestClient(main.app) as client:
        assert client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'synthetic-full-opening','password':'Synthetic-Only-HTTP-1712!'},follow_redirects=False).status_code==303
        csrf=token(client.get('/leave/opening'))
        group_page=client.post('/leave/opening/review',data=dict(csrf_token=csrf,employee_id=eid,effective_start='2026-05-17',annual='8.25',weekly='2',holiday='0',reason='Synthetic full UI'))
        assert group_page.status_code==200
        group_id=str(group_page.url).rsplit('/',1)[1]
        policy_page=client.post('/leave/opening/policy-defaults',data=dict(csrf_token=csrf,group_id=group_id))
        assert policy_page.status_code==200
        policy_id=str(policy_page.url).split('policy_id=')[1]
        digest=re.search(r'name="source_sha256" value="([^"]+)"',policy_page.text).group(1)
        source_page=client.post('/leave/opening/confirm-source',data=dict(csrf_token=csrf,group_id=group_id,policy_id=policy_id,source_sha256=digest,previous_id='',reason='Synthetic source confirmation',human_confirmed='confirmed'))
        assert source_page.status_code==200
        apply_fields={k:re.search('name="'+k+'" value="([^"]*)"',source_page.text).group(1) for k in ('selection_id','reviewed_apply_sha256')}
        final=client.post('/leave/opening/confirm-source',data=dict(csrf_token=csrf,group_id=group_id,policy_id=policy_id,operation_stage='apply',response_format='html',reason='Synthetic final opening',human_confirmed='confirmed',**apply_fields))
        assert final.status_code==200 and 'name="reviewed_apply_sha256"' not in final.text
        assert client.post('/leave/opening/confirm-source',data=dict(csrf_token=csrf,group_id=group_id,policy_id=policy_id,operation_stage='apply',reason='Synthetic final opening',human_confirmed='confirmed',**apply_fields)).json()['replayed']
    with SessionLocal() as db:
        rows=db.execute(text('SELECT days_delta,earning_year,released_on FROM leave_balance_transactions WHERE employee_id=:e ORDER BY id'),dict(e=eid)).all()
        assert rows==[(Decimal('8.25'),None,None),(Decimal('2'),None,None)]
