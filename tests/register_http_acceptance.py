"""Actual normal-name PostgreSQL HTTP, no synthetic feature flags/auth override."""
import os,re
from datetime import date
import pytest
from sqlalchemy import select,func,text
from fastapi.testclient import TestClient
from app import main
from app.config import settings
from app.db import SessionLocal
from app.models import AdminAccount,LeaveRegisterEntry,LeaveRegisterConsumption,LeaveBalanceTransaction,PayrollItem,LeaveMonthClose,AuditLog
from app.security import hash_password
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_return_register_cycle import setup_return_chain
from tests.test_shift_day_http import token

def test_normal_postgres_reviewed_register_end_to_end(history_context,monkeypatch):
    lang=os.environ["REGISTER_ACCEPTANCE_LANG"]
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    db,actor,auth,_fact=history_context
    actor.password_hash=hash_password('Synthetic-Monthly-Only-1500!')
    db.add(AdminAccount(username='synthetic-month-assistant-'+lang,password_hash=hash_password('Synthetic-Monthly-Only-1500!'),role='assistant_manager',active=True))
    db.add(AdminAccount(username='synthetic-month-finance-'+lang,password_hash=hash_password('Synthetic-Monthly-Only-1500!'),role='finance',active=True))
    db.flush();db.commit()
    assert not settings.leave_synthetic_register_enabled
    assert not settings.attendance_synthetic_review_enabled
    assert not main.app.dependency_overrides
    monkeypatch.setattr(settings,'app_env','test')
    def counts():
        with SessionLocal() as check:return tuple(check.scalar(select(func.count()).select_from(m)) for m in (LeaveRegisterEntry,LeaveRegisterConsumption,LeaveBalanceTransaction,PayrollItem,LeaveMonthClose))
    before=counts()
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',lang)
        assert client.get('/leave/month-review').status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=actor.username,password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        import base64,json
        from itsdangerous import TimestampSigner
        session=json.loads(base64.b64decode(TimestampSigner(settings.session_secret).unsign(client.cookies.get('session'))))
        auth['session_id']=session['admin_session_id']
        ctx=setup_return_chain(history_context,multi=True,consume_attendance=False);employee=ctx['employee'];db.commit()

        attendance_root=f'/attendance/shift-evidence/{ctx["compared"].id}/{employee.id}'
        detail=client.get(attendance_root,params={'work_date':'2026-05-30'})
        assert detail.status_code==200
        args=dict(csrf_token=token(detail),operation_id='1'*64,work_date='2026-05-30',windows=f'2026-05-30:{ctx["pattern"].id}',selected_window=f'2026-05-30:{ctx["pattern"].id}',in_ordinal='0',out_ordinal='2',reason='Synthetic actual inline endpoints')
        if os.name not in ('posix','nt'):
            assert client.post(attendance_root+'/review',data=args).status_code==503
            unavailable=client.get('/leave/month-review',params={'month':'2026-05'})
            assert unavailable.status_code==200 and 'register-summary' not in unavailable.text
            data=dict(csrf_token=token(unavailable),month='2026-05',comparison_id=ctx['compared'].id,employee_id=str(employee.id),reason='Synthetic Windows unavailable')
            assert client.post('/leave/month-review/register-review',data=data).status_code==503
            assert counts()==before
            return
        reviewed_attendance=client.post(attendance_root+'/review',data=args)
        assert reviewed_attendance.status_code==200
        digest=re.search('name="request_digest" value="([^"]+)"',reviewed_attendance.text).group(1)
        registered_attendance=client.post(attendance_root+'/confirm',data=dict(csrf_token=token(reviewed_attendance),receipt_id='1'*64,request_digest=digest,human_confirmed='yes'))
        assert registered_attendance.status_code==200 and 'attendance-registered-pending' in registered_attendance.text
        page=client.get('/leave/month-review',params={'month':'2026-05'})
        assert page.status_code==200 and page.headers['cache-control']=='no-store'
        assert 'name="comparison_id"' in page.text and ctx['compared'].id in page.text
        from pathlib import Path
        import tempfile
        capture=Path(os.environ.get('REGISTER_CAPTURE_DIR') or tempfile.mkdtemp(prefix='register-http-'));capture.mkdir(parents=True,exist_ok=True)
        (capture/(lang+'.html')).write_text(page.text)
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.get('/leave/month-review',params={'month':'2026-05'})
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',re.sub(r'<bdi>.*?</bdi>','',content)) is None, [(m.start(),content[max(0,m.start()-50):m.start()+130]) for m in re.finditer(r'[\u0600-\u06ff]+',content)]
            (capture/(lang+'.html')).write_text(localized.text)
        client.cookies.set('my_hr_language','ar')
        assert client.get('/leave/month-review',params={'month':'2026-5'}).status_code==400
        assert client.get('/leave/month-review',params={'year':'2026','month_number':'05'}).status_code==200
        assert client.get('/leave/month-review',params={'year':'2026','month_number':'13'}).status_code==400
        assert client.get('/leave/month-review',params={'year':'2026','month_number':'05','month':'2026-06'}).status_code==400
        data=dict(csrf_token=token(page),month='2026-05',comparison_id=ctx['compared'].id,employee_id=str(employee.id),reason='Synthetic reviewed returned days')
        assert client.post('/leave/month-review/register-review',data={k:v for k,v in data.items() if k!='csrf_token'}).status_code==403
        assert client.post('/leave/month-review/register-review',data={**data,'extra':'1'}).status_code==400
        assert client.post('/leave/month-review/register-review',data={**data,'month':'2026-06'}).status_code==400
        import time
        import app.leave_month_review_routes as routes
        from sqlalchemy import text
        original=routes.preview_return_register
        def busy(db,**kw):
            with SessionLocal() as holder:
                holder.execute(text("SET LOCAL lock_timeout='500ms'"));holder.execute(text('LOCK TABLE attendance_daily_records IN ACCESS EXCLUSIVE MODE'))
                return original(db,**kw)
        monkeypatch.setattr(routes,'preview_return_register',busy)
        started=time.monotonic()
        busy_response=client.post('/leave/month-review/register-review',data=data)
        assert busy_response.status_code==503 and busy_response.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5
        assert counts()==before
        monkeypatch.setattr(routes,'preview_return_register',original)
        reviewed=client.post('/leave/month-review/register-review',data=data)
        assert reviewed.status_code==200 and 'id="register-summary"' in reviewed.text
        assert 'الملخص معروض؛ لم يتم التسجيل بعد' in reviewed.text and 'إجازة سنوية' in reviewed.text
        (capture/'review-ar.html').write_text(reviewed.text)
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.post('/leave/month-review/register-review',data=data)
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',re.sub(r'<bdi>.*?</bdi>','',content)) is None, [(m.start(),content[max(0,m.start()-50):m.start()+130]) for m in re.finditer(r'[\u0600-\u06ff]+',content)]
            (capture/('review-'+lang+'.html')).write_text(localized.text)
        client.cookies.set('my_hr_language','ar')
        proof=re.search('name="proof" value="([^"]+)"',reviewed.text).group(1)
        assert counts()==before
        confirm=dict(csrf_token=token(reviewed),proof=proof,human_confirmed='yes')
        assert client.post('/leave/month-review/register-confirm',data={**confirm,'proof':proof+'x'}).status_code==400
        assert client.post('/leave/month-review/register-confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        from app.models import EmployeeServicePeriod
        with SessionLocal.begin() as changed:
            period=changed.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee.id))
            period.end_date=date(2026,5,30)
        assert client.post('/leave/month-review/register-confirm',data=confirm).status_code==400 and counts()==before
        with SessionLocal.begin() as changed:
            period=changed.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id==employee.id))
            period.end_date=None
        original_consume=routes.consume_return_register
        def confirm_busy(db,**kw):
            with SessionLocal() as holder:
                holder.execute(text("SET LOCAL lock_timeout='500ms'"));holder.execute(text('LOCK TABLE attendance_daily_records IN ACCESS EXCLUSIVE MODE'))
                return original_consume(db,**kw)
        monkeypatch.setattr(routes,'consume_return_register',confirm_busy)
        started=time.monotonic()
        busy_confirm=client.post('/leave/month-review/register-confirm',data=confirm)
        assert busy_confirm.status_code==503 and busy_confirm.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5 and counts()==before
        monkeypatch.setattr(routes,'consume_return_register',original_consume)
        original_template=main.templates.TemplateResponse
        def render_failure(*a,**kw):
            raise ValueError('Synthetic render failure after registration before commit')
        monkeypatch.setattr(main.templates,'TemplateResponse',render_failure)
        assert client.post('/leave/month-review/register-confirm',data=confirm).status_code==400 and counts()==before
        monkeypatch.setattr(main.templates,'TemplateResponse',original_template)
        result=client.post('/leave/month-review/register-confirm',data=confirm)
        assert result.status_code==200
        (capture/'saved-ar.html').write_text(result.text)
        for output_lang in ('en','zh'):
            client.cookies.set('my_hr_language',output_lang)
            (capture/('saved-'+output_lang+'.html')).write_text(client.get('/leave/month-review',params={'month':'2026-05'}).text)
        client.cookies.set('my_hr_language','ar')
        after=counts();assert after[:2]==(before[0]+2,before[1]+1) and after[2:]==before[2:]
        assert client.post('/leave/month-review/register-confirm',data=confirm).status_code==200 and counts()==after
        assert client.post('/leave/month-review/close',data={'csrf_token':token(result)}).status_code in (404,405)
        assert client.post('/login',data=dict(csrf_token=token(result),username=actor.username,password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        fresh=client.get('/leave/month-review',params={'month':'2026-05'})
        assert client.post('/leave/month-review/register-confirm',data={**confirm,'csrf_token':token(fresh)}).status_code==400 and counts()==after

        # A receipt does not expand operator scope. Real second-role login and CSRF.
        for role in ('assistant','finance'):
            assert client.post('/login',data=dict(csrf_token=token(fresh),username='synthetic-month-'+role+'-'+os.environ['REGISTER_ACCEPTANCE_LANG'],password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
            assert client.get('/leave/month-review',params={'month':'2026-05'}).status_code==403
            assert client.post('/leave/month-review/register-review',data=data).status_code in (403,400)
            assert client.post(attendance_root+'/review',data=args).status_code in (403,400)
            fresh=client.get('/login')
        with SessionLocal() as verify:
            from sqlalchemy.exc import DBAPIError
            for sql in ('UPDATE leave_register_consumptions SET source_digest=source_digest','DELETE FROM leave_register_consumptions','TRUNCATE leave_register_consumptions'):
                with pytest.raises(DBAPIError):
                    with verify.begin_nested():verify.execute(text(sql))
            assert verify.scalar(select(func.count()).select_from(LeaveRegisterConsumption))==1
