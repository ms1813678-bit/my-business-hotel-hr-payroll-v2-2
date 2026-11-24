"""Real auth/middleware/CSRF monthly selected UI, dedicated synthetic_cycle DB."""
import os,re
from datetime import date
import pytest
from sqlalchemy import select,func
from fastapi.testclient import TestClient
from app import main
from app.config import settings
from app.db import SessionLocal
from app.models import AdminAccount,LeaveRegisterEntry,LeaveSyntheticRegisterConsumption,LeaveBalanceTransaction,PayrollItem,LeaveMonthClose,AuditLog
from app.security import hash_password
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_return_register_cycle import setup_return_chain
from tests.test_shift_day_http import token

pytestmark=pytest.mark.skipif(os.environ.get('MANUAL_MONTH_REVIEW_HTTP')!='1',reason='Dedicated committed synthetic_cycle HTTP fixture')


def test_selected_month_review_real_http_register_and_unknown_close_block(history_context,monkeypatch):
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    db,actor,auth,_fact=history_context
    actor.password_hash=hash_password('Synthetic-Monthly-Only-1500!')
    db.add(AdminAccount(username='synthetic-month-assistant',password_hash=hash_password('Synthetic-Monthly-Only-1500!'),role='assistant_manager',active=True))
    db.add(AdminAccount(username='synthetic-month-finance',password_hash=hash_password('Synthetic-Monthly-Only-1500!'),role='finance',active=True))
    db.flush();db.commit()
    monkeypatch.setattr(settings,'leave_synthetic_register_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    def counts():
        with SessionLocal() as check:return tuple(check.scalar(select(func.count()).select_from(m)) for m in (LeaveRegisterEntry,LeaveSyntheticRegisterConsumption,LeaveBalanceTransaction,PayrollItem,LeaveMonthClose))
    before=counts()
    with TestClient(main.app) as client:
        assert client.get('/leave/month-review').status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=actor.username,password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        import base64,json
        from itsdangerous import TimestampSigner
        session=json.loads(base64.b64decode(TimestampSigner(settings.session_secret).unsign(client.cookies.get('session'))))
        auth['session_id']=session['admin_session_id']
        ctx=setup_return_chain(history_context,multi=True,consume_attendance=False);employee=ctx['employee'];db.commit()
        monkeypatch.setattr(settings,'attendance_synthetic_review_enabled',True)
        attendance_root=f'/attendance/shift-evidence/{ctx["compared"].id}/{employee.id}'
        detail=client.get(attendance_root,params={'work_date':'2026-05-30'})
        assert detail.status_code==200
        args=dict(csrf_token=token(detail),operation_id='1'*64,work_date='2026-05-30',windows=f'2026-05-30:{ctx["pattern"].id}',selected_window=f'2026-05-30:{ctx["pattern"].id}',in_ordinal='0',out_ordinal='2',reason='Synthetic actual inline endpoints')
        reviewed_attendance=client.post(attendance_root+'/review',data=args)
        assert reviewed_attendance.status_code==200
        digest=re.search('name="request_digest" value="([^"]+)"',reviewed_attendance.text).group(1)
        registered_attendance=client.post(attendance_root+'/confirm',data=dict(csrf_token=token(reviewed_attendance),receipt_id='1'*64,request_digest=digest,human_confirmed='yes'))
        assert registered_attendance.status_code==200 and 'attendance-registered-pending' in registered_attendance.text
        page=client.get('/leave/month-review',params={'month':'2026-05'})
        assert page.status_code==200 and page.headers['cache-control']=='no-store'
        assert 'معاينة فقط، التنفيذ معطّل' in page.text and 'الرصيد المتاح: غير محسوم' in page.text
        assert 'name="comparison_id"' in page.text and ctx['compared'].id in page.text
        from pathlib import Path
        capture=Path('/downloads/month-review-pixels');capture.mkdir(exist_ok=True)
        (capture/'ar.html').write_text(page.text)
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
        original=routes.preview_synthetic_register
        def busy(db,**kw):
            with SessionLocal() as holder:
                holder.execute(text("SET LOCAL lock_timeout='500ms'"));holder.execute(text('LOCK TABLE attendance_daily_records IN ACCESS EXCLUSIVE MODE'))
                return original(db,**kw)
        monkeypatch.setattr(routes,'preview_synthetic_register',busy)
        started=time.monotonic()
        busy_response=client.post('/leave/month-review/register-review',data=data)
        assert busy_response.status_code==503 and busy_response.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5
        assert counts()==before
        monkeypatch.setattr(routes,'preview_synthetic_register',original)
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
        original_consume=routes.consume_synthetic_register
        def confirm_busy(db,**kw):
            with SessionLocal() as holder:
                holder.execute(text("SET LOCAL lock_timeout='500ms'"));holder.execute(text('LOCK TABLE attendance_daily_records IN ACCESS EXCLUSIVE MODE'))
                return original_consume(db,**kw)
        monkeypatch.setattr(routes,'consume_synthetic_register',confirm_busy)
        started=time.monotonic()
        busy_confirm=client.post('/leave/month-review/register-confirm',data=confirm)
        assert busy_confirm.status_code==503 and busy_confirm.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5 and counts()==before
        monkeypatch.setattr(routes,'consume_synthetic_register',original_consume)
        original_template=main.templates.TemplateResponse
        def render_failure(*a,**kw):
            raise ValueError('Synthetic render failure after registration before commit')
        monkeypatch.setattr(main.templates,'TemplateResponse',render_failure)
        assert client.post('/leave/month-review/register-confirm',data=confirm).status_code==400 and counts()==before
        monkeypatch.setattr(main.templates,'TemplateResponse',original_template)
        result=client.post('/leave/month-review/register-confirm',data=confirm)
        assert result.status_code==200 and 'تم تسجيل أيام الملف' in result.text
        after=counts();assert after[:2]==(before[0]+2,before[1]+1) and after[2:]==before[2:]
        assert client.post('/leave/month-review/register-confirm',data=confirm).status_code==200 and counts()==after
        assert client.post('/leave/month-review/close',data={'csrf_token':token(result)}).status_code in (404,405)
        assert client.post('/login',data=dict(csrf_token=token(result),username=actor.username,password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        fresh=client.get('/leave/month-review',params={'month':'2026-05'})
        assert client.post('/leave/month-review/register-confirm',data={**confirm,'csrf_token':token(fresh)}).status_code==400 and counts()==after
        # Evidence editor is read/compute only, including complete fixture assertions.
        from app.models import LeaveType
        from decimal import Decimal
        with SessionLocal.begin() as setup:
            for kind in setup.scalars(select(LeaveType).where(LeaveType.code.in_(('annual','weekly_rest','official_holiday')))):
                kind.tracks_balance=True
            kind=setup.scalar(select(LeaveType).where(LeaveType.code=='annual'))
            legacy=LeaveBalanceTransaction(employee_id=employee.id,leave_type_id=kind.id,transaction_type='adjustment',days_delta=Decimal('-0.12345678901234567890123456789'),reason='Synthetic external evidence',created_by='synthetic')
            setup.add(legacy);setup.flush();legacy_id=legacy.id;kind_id=kind.id
        editor_before=counts()
        with SessionLocal() as checked:audit_before=checked.scalar(select(func.count()).select_from(AuditLog))
        editor_root=f'/leave/month-review/reconciliation/{ctx["compared"].id}/{employee.id}'
        editor=client.get(editor_root)
        assert editor.status_code==200 and 'مسودة تسوية الأرصدة' in editor.text
        fields=dict(csrf_token=token(editor),comparison_id=ctx['compared'].id,employee_id=str(employee.id),release_evaluated_on='2026-05-31')
        assert client.post('/leave/month-review/reconciliation-snapshot',data={**fields,'release_evaluated_on':'2026-05-30'}).status_code==400
        import app.leave_reconciliation_editor_routes as editor_routes
        original_reconcile=editor_routes.reconcile_selected_opening
        def editor_busy(db,**kw):
            with SessionLocal() as holder:
                holder.execute(text("SET LOCAL lock_timeout='500ms'"));holder.execute(text('LOCK TABLE leave_balance_transactions IN ACCESS EXCLUSIVE MODE'))
                return original_reconcile(db,**kw)
        monkeypatch.setattr(editor_routes,'reconcile_selected_opening',editor_busy)
        started=time.monotonic()
        locked_editor=client.post('/leave/month-review/reconciliation-snapshot',data=fields)
        assert locked_editor.status_code==503 and locked_editor.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5
        monkeypatch.setattr(editor_routes,'reconcile_selected_opening',original_reconcile)
        snapshot=client.post('/leave/month-review/reconciliation-snapshot',data=fields)
        assert snapshot.status_code==200 and 'name="opening_A_value"' in snapshot.text and f'name="ledger_{legacy_id}_value"' in snapshot.text
        editor_proof=re.search('name="proof" value="([^"]+)"',snapshot.text).group(1)
        draft_fields=dict(csrf_token=token(snapshot),proof=editor_proof)
        for code in ('A','H','W'):
            draft_fields[f'opening_{code}_value']='unknown'
            draft_fields[f'opening_{code}_reference']='Synthetic evidence missing'
        draft_fields[f'ledger_{legacy_id}_value']='unresolved';draft_fields[f'ledger_{legacy_id}_reference']='Synthetic unknown'
        unknown=client.post('/leave/month-review/reconciliation-draft',data=draft_fields)
        assert unknown.status_code==200 and 'توجد أدلة غير محسومة أو تعارضات' in unknown.text
        assert 'هدف افتراضي بالأيام' not in unknown.text
        assert client.post('/leave/month-review/reconciliation-draft',data={**draft_fields,'unexpected':'1'}).status_code==400
        assert client.post('/leave/month-review/reconciliation-draft',data={**draft_fields,'proof':editor_proof+'x'}).status_code==400
        assert client.post('/leave/month-review/reconciliation-draft',data={k:v for k,v in draft_fields.items() if k!=f'ledger_{legacy_id}_reference'}).status_code==400
        for code in ('A','H','W'):
            draft_fields[f'opening_{code}_value']='released'
            draft_fields[f'opening_{code}_reference']='Synthetic fixture release assertion only'
        draft_fields[f'ledger_{legacy_id}_value']='external_to_scenario'
        drafted=client.post('/leave/month-review/reconciliation-draft',data=draft_fields)
        assert drafted.status_code==200 and 'هدف افتراضي بالأيام' in drafted.text
        (capture/'editor-ar.html').write_text(snapshot.text);(capture/'draft-ar.html').write_text(drafted.text)
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.post('/leave/month-review/reconciliation-snapshot',data=fields)
            assert localized.status_code==200
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',content) is None,content[4500:4750]
            (capture/('editor-'+lang+'.html')).write_text(localized.text)
            localized_draft=client.post('/leave/month-review/reconciliation-draft',data=draft_fields)
            assert localized_draft.status_code==200
            content=localized_draft.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',content) is None
            (capture/('draft-'+lang+'.html')).write_text(localized_draft.text)
        client.cookies.set('my_hr_language','ar')
        assert counts()==editor_before
        with SessionLocal() as checked:assert checked.scalar(select(func.count()).select_from(AuditLog))==audit_before
        from app.models import LeaveReconciliationReviewReceipt
        record_proof=re.search('name="record_proof" value="([^"]+)"',drafted.text).group(1)
        record_fields=dict(csrf_token=token(drafted),record_proof=record_proof,reason='Synthetic record operator assertions only',human_confirmed='yes')
        assert client.post('/leave/month-review/reconciliation-record',data={k:v for k,v in record_fields.items() if k!='human_confirmed'}).status_code==400
        assert client.post('/leave/month-review/reconciliation-record',data={**record_fields,'record_proof':record_proof+'x'}).status_code==400
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        def fail_receipt_audit(session,*_):
            if any(isinstance(row,AuditLog) and row.action=='leave_reconciliation_assertions_recorded' for row in session.new):
                raise ValueError('Synthetic receipt audit failure')
        event.listen(Session,'before_flush',fail_receipt_audit)
        try:
            assert client.post('/leave/month-review/reconciliation-record',data=record_fields).status_code==400
        finally:event.remove(Session,'before_flush',fail_receipt_audit)
        with SessionLocal() as checked:assert checked.scalar(select(func.count()).select_from(LeaveReconciliationReviewReceipt))==0
        recorded=client.post('/leave/month-review/reconciliation-record',data=record_fields)
        assert recorded.status_code==200 and 'reconciliation-assertions-recorded' in recorded.text
        receipt_id=re.search('/leave/month-review/reconciliation-receipt/([0-9a-f]{64})',recorded.text).group(1)
        assert client.post('/leave/month-review/reconciliation-record',data=record_fields).status_code==200
        assert receipt_id in client.get('/leave/month-review',params={'month':'2026-05'}).text
        historical=client.get('/leave/month-review/reconciliation-receipt/'+receipt_id)
        assert historical.status_code==200 and 'هذا سجل تاريخي' in historical.text
        (capture/'receipt-ar.html').write_text(historical.text)
        assert counts()==editor_before
        with SessionLocal() as checked:
            assert checked.scalar(select(func.count()).select_from(LeaveReconciliationReviewReceipt))==1
            assert checked.scalar(select(func.count()).select_from(AuditLog))==audit_before+1
            from sqlalchemy.exc import DBAPIError
            for statement in ('UPDATE leave_reconciliation_review_receipts SET reason=reason','DELETE FROM leave_reconciliation_review_receipts'):
                with pytest.raises(DBAPIError):
                    with checked.begin_nested():checked.execute(text(statement))
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.get('/leave/month-review/reconciliation-receipt/'+receipt_id)
            assert localized.status_code==200
            assert re.search(r'[\u0600-\u06ff]',localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]) is None
            (capture/('receipt-'+lang+'.html')).write_text(localized.text)
        client.cookies.set('my_hr_language','ar')

        preview_before=counts()
        with SessionLocal() as checked:
            preview_audit=checked.scalar(select(func.count()).select_from(AuditLog))
        for stage in ('readiness','summary'):
            for lang in ('ar','en','zh'):
                client.cookies.set('my_hr_language',lang)
                preview=client.get('/leave/month-review',params={'month':'2026-05','close_stage':stage})
                assert preview.status_code==200 and preview.headers['cache-control']=='no-store'
                body=preview.text.split('id="month-close"',1)[1].split('</section>',1)[0]
                if stage=='summary':assert 'disabled' in body
                assert '<form' not in body and 'method="post"' not in body
                if lang!='ar':assert re.search(r'[\u0600-\u06ff]',body) is None,body
                (capture/f'close-{stage}-{lang}.html').write_text(preview.text)
        client.cookies.set('my_hr_language','ar')
        assert client.get('/leave/month-review',params={'month':'2026-05','close_stage':'execute'}).status_code==400
        assert client.post('/leave/month-review/close-confirm',data={}).status_code==404
        assert counts()==preview_before
        with SessionLocal() as checked:
            assert checked.scalar(select(func.count()).select_from(AuditLog))==preview_audit
        assert client.post('/login',data=dict(csrf_token=token(drafted),username=actor.username,password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        new_editor=client.get(editor_root)
        assert client.post('/leave/month-review/reconciliation-draft',data={**draft_fields,'csrf_token':token(new_editor)}).status_code==400
        fields['csrf_token']=token(new_editor)
        refreshed=client.post('/leave/month-review/reconciliation-snapshot',data=fields)
        assert refreshed.status_code==200
        draft_fields['proof']=re.search('name="proof" value="([^"]+)"',refreshed.text).group(1)
        draft_fields['csrf_token']=token(refreshed)
        with SessionLocal.begin() as changed:
            changed.add(LeaveBalanceTransaction(employee_id=employee.id,leave_type_id=kind_id,transaction_type='adjustment',days_delta=Decimal(1),reason='Synthetic new evidence',created_by='synthetic'))
        assert client.post('/leave/month-review/reconciliation-draft',data=draft_fields).status_code==400
        assert client.get('/leave/month-review/reconciliation-receipt/'+receipt_id).status_code==200
        monkeypatch.setattr(settings,'leave_synthetic_register_enabled',False)
        assert client.post('/leave/month-review/register-review',data={**data,'csrf_token':token(new_editor)}).status_code==404
    with TestClient(main.app) as client:
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username='synthetic-month-finance',password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        assert client.get('/leave/month-review',params={'month':'2026-05'}).status_code==403
        assert client.get(editor_root).status_code==403
        handoff='/leave/payroll-day-handoff'
        report=client.get(handoff,params={'employee_id':employee.id,'month':'2026-05'})
        assert report.status_code==200 and 'تغطية السجل مكتملة، ليست اعتمادًا نهائيًا' in report.text and report.headers['cache-control']=='no-store'
        (capture/'handoff-ar.html').write_text(report.text)
        handoff_before=counts()
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.get(handoff,params={'employee_id':employee.id,'month':'2026-05'})
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',content) is None,[(m.start(),content[m.start()-30:m.start()+120]) for m in re.finditer(r'[\u0600-\u06ff]+',content)]
            (capture/('handoff-'+lang+'.html')).write_text(localized.text)
        client.cookies.set('my_hr_language','ar')
        assert client.post(handoff,data={}).status_code==405
        with SessionLocal.begin() as changed:
            entry=changed.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee.id).order_by(LeaveRegisterEntry.work_date))
            entry.code=None
        unknown_days=client.get(handoff,params={'employee_id':employee.id,'month':'2026-05'})
        assert unknown_days.status_code==200 and 'تسليم الأيام غير مكتمل' in unknown_days.text
        assert counts()==handoff_before
    with TestClient(main.app) as assistant:
        assert assistant.post('/login',data=dict(csrf_token=token(assistant.get('/login')),username='synthetic-month-assistant',password='Synthetic-Monthly-Only-1500!'),follow_redirects=False).status_code==303
        assert assistant.get('/leave/payroll-day-handoff',params={'employee_id':employee.id,'month':'2026-05'}).status_code==403
