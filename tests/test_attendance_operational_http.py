"""Dedicated synthetic DB only, real middleware/auth/CSRF and commits."""
import os,re
from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app import main
from app.config import settings
from app.db import SessionLocal
from app.models import AttendanceMonth,EmployeeServicePeriod,AttendanceDailyRecord,AttendanceEvidenceReviewReceipt,AttendanceEvidenceConsumption,AttendanceEvidenceOccurrenceClaim,LeaveBalanceTransaction,PayrollItem,AdminAccount,DepartmentFingerprintPreparation,AdminSession,SystemSetting
from tests.test_shift_day_http import seed,token

pytestmark=pytest.mark.skipif(os.getenv('MANUAL_ATTENDANCE_HTTP')!='1',reason='Dedicated synthetic_attendance DB required')


def test_inline_review_confirm_actual_middleware_atomic_idempotent(monkeypatch):
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None);monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    monkeypatch.setattr(settings,'attendance_synthetic_review_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    employee,pattern=seed()
    with SessionLocal.begin() as db:
        db.add(EmployeeServicePeriod(employee_id=employee,start_date=date(2025,1,1)))
        db.add(AttendanceMonth(period_start=date(2026,11,1),period_end=date(2026,11,30),status='under_review',created_by='synthetic'))
    root=f'/attendance/shift-evidence/{"d"*64}/{employee}'
    with TestClient(main.app) as client:
        assert client.post(root+'/review',data={}).status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username='synthetic-shift-system_admin',password='Synthetic-Only-Login-1004!'),follow_redirects=False).status_code==303
        detail=client.get(root,params={'work_date':'2026-11-01'})
        compared=client.post(root,data=dict(csrf_token=token(detail),work_date='2026-11-01',windows=f'2026-11-01:{pattern}'))
        assert compared.status_code==200 and 'name="in_ordinal"' in compared.text
        args=dict(csrf_token=token(compared),operation_id='f'*64,work_date='2026-11-01',windows=f'2026-11-01:{pattern}',selected_window=f'2026-11-01:{pattern}',in_ordinal='0',out_ordinal='2',reason='Synthetic actual inline reviewed endpoints')
        assert client.post(root+'/review',data={k:v for k,v in args.items() if k!='csrf_token'}).status_code==403
        for change in (dict(extra='1'),dict(in_ordinal='2'),dict(work_date='2026-11-02')):
            assert client.post(root+'/review',data={**args,**change}).status_code==400
        import time
        for path in (root.upper().replace('/ATTENDANCE/SHIFT-EVIDENCE/','/attendance/shift-evidence/')+'/review',root.replace(f'/{employee}',f'/0{employee}')+'/review',root+'/review%0A'):
            started=time.monotonic();response=client.post(path,data=args)
            assert response.status_code in (400,404) and time.monotonic()-started<2
        # After dependency authentication, exercise existing read and prepare
        # gate waits with actual independent PostgreSQL lock holders.
        from sqlalchemy import text
        from app.restore_lock import RESTORE_LOCK_KEY
        import app.shift_day_routes as routes
        def locked_call(original,read=False):
            def blocked(db,**kwargs):
                with SessionLocal() as holder:
                    if read:holder.execute(text('LOCK TABLE department_fingerprint_preparations IN ACCESS EXCLUSIVE MODE'))
                    else:holder.execute(text('SELECT pg_advisory_lock(:key)'),{'key':RESTORE_LOCK_KEY})
                    try:return original(db,**kwargs)
                    finally:
                        if not read:holder.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':RESTORE_LOCK_KEY})
            return blocked
        def no_effect_counts():
            with SessionLocal() as check:
                return tuple(check.scalar(select(func.count()).select_from(model)) for model in (AttendanceEvidenceReviewReceipt,AttendanceDailyRecord,AttendanceEvidenceConsumption,AttendanceEvidenceOccurrenceClaim))
        before_wait=no_effect_counts()
        original_read=routes.read_saved_shift_days
        monkeypatch.setattr(routes,'read_saved_shift_days',locked_call(original_read,read=True))
        started=time.monotonic();read_busy=client.get(root,params={'work_date':'2026-11-01'})
        assert read_busy.status_code==404 and 2.5<time.monotonic()-started<5
        assert no_effect_counts()==before_wait
        started=time.monotonic();preview_busy=client.post(root,data=dict(csrf_token=token(compared),work_date='2026-11-01',windows=f'2026-11-01:{pattern}'))
        assert preview_busy.status_code==400 and 2.5<time.monotonic()-started<5
        assert no_effect_counts()==before_wait
        monkeypatch.setattr(routes,'read_saved_shift_days',original_read)
        original_prepare=routes.prepare_evidence_review
        monkeypatch.setattr(routes,'prepare_evidence_review',locked_call(original_prepare))
        started=time.monotonic();prepare_busy=client.post(root+'/review',data=args)
        assert prepare_busy.status_code==503 and prepare_busy.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5
        assert no_effect_counts()==before_wait
        monkeypatch.setattr(routes,'prepare_evidence_review',original_prepare)
        reviewed=client.post(root+'/review',data=args)
        assert '<input' not in reviewed.text.split('<title>',1)[1].split('</title>',1)[0]
        assert reviewed.status_code==200 and 'attendance-review-summary' in reviewed.text and reviewed.headers['cache-control']=='no-store'
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.post(root+'/review',data=args)
            assert localized.status_code==200
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',content) is None
        client.cookies.set('my_hr_language','ar')
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(AttendanceDailyRecord))==0
            row=db.get(AttendanceEvidenceReviewReceipt,'f'*64);digest=row.request_digest
            financial=tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem))
        confirm=dict(csrf_token=token(reviewed),receipt_id='f'*64,request_digest=digest,human_confirmed='yes')
        assert client.post(root+'/confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        assert client.post(root+'/confirm',data={**confirm,'request_digest':'0'*64}).status_code==400
        assert client.post(root.replace(f'/{employee}',f'/{employee+10000}')+'/confirm',data=confirm).status_code==400
        from sqlalchemy import text
        from app.restore_lock import RESTORE_LOCK_KEY
        import app.shift_day_routes as routes
        original_consume=routes.consume_evidence_review
        def contention(db,**kwargs):
            # Trigger restore gate after HTTP dependency authentication has ended,
            # exercising inherited service3s timeout without blocking auth first.
            with SessionLocal() as blocker:
                blocker.execute(text('SELECT pg_advisory_lock(:key)'),{'key':RESTORE_LOCK_KEY})
                try:return original_consume(db,**kwargs)
                finally:blocker.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':RESTORE_LOCK_KEY})
        monkeypatch.setattr(routes,'consume_evidence_review',contention)
        started=time.monotonic();busy=client.post(root+'/confirm',data=confirm)
        assert busy.status_code==503 and busy.headers['retry-after']=='3' and 2.5<time.monotonic()-started<5
        monkeypatch.setattr(routes,'consume_evidence_review',original_consume)
        assert client.post(root+'/confirm',data={**confirm,'human_confirmed':'no'}).status_code==400
        duplicate='&'.join(f'{k}={v}' for k,v in confirm.items())+'&receipt_id='+('f'*64)
        assert client.post(root+'/confirm',content=duplicate,headers={'content-type':'application/x-www-form-urlencoded'}).status_code==400
        for env in ('production','development','prod','unknown'):
            monkeypatch.setattr(settings,'app_env',env)
            assert client.post(root+'/confirm',data=confirm).status_code==404
        monkeypatch.setattr(settings,'app_env','test')
        original_url=settings.database_url
        monkeypatch.setattr(settings,'database_url',original_url.replace('synthetic_attendance_','not_synthetic_'))
        assert client.post(root+'/confirm',data=confirm).status_code==404
        monkeypatch.setattr(settings,'database_url',original_url)
        with SessionLocal.begin() as db:
            saved=db.get(AttendanceEvidenceReviewReceipt,'f'*64)
            other=AdminAccount(username='synthetic-other-reviewer',password_hash='not-a-login',role='system_admin',active=True);db.add(other);db.flush()
            original_actor=saved.actor_id;saved.actor_id=other.id
        assert client.post(root+'/confirm',data=confirm).status_code==400
        with SessionLocal.begin() as db:db.get(AttendanceEvidenceReviewReceipt,'f'*64).actor_id=original_actor
        with SessionLocal.begin() as db:
            source=db.get(DepartmentFingerprintPreparation,'d'*64);original_bytes=source.fingerprint_bytes;source.fingerprint_bytes+=b'changed'
        assert client.post(root+'/confirm',data=confirm).status_code==400
        with SessionLocal.begin() as db:db.get(DepartmentFingerprintPreparation,'d'*64).fingerprint_bytes=original_bytes
        with SessionLocal.begin() as db:
            saved=db.get(AttendanceEvidenceReviewReceipt,'f'*64);saved.restore_epoch='old-synthetic'
        assert client.post(root+'/confirm',data=confirm).status_code==400
        with SessionLocal.begin() as db:db.get(AttendanceEvidenceReviewReceipt,'f'*64).restore_epoch=None
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(AttendanceDailyRecord))==0
            assert db.scalar(select(func.count()).select_from(AttendanceEvidenceConsumption))==0
        done=client.post(root+'/confirm',data=confirm)
        assert done.status_code==200 and 'attendance-registered-pending' in done.text
        assert client.post(root+'/confirm',data=confirm).status_code==200
        with SessionLocal() as db:
            record=db.scalar(select(AttendanceDailyRecord));assert record.status=='needs_review' and record.worked_minutes==480
            assert db.scalar(select(func.count()).select_from(AttendanceDailyRecord))==1
            assert db.scalar(select(func.count()).select_from(AttendanceEvidenceConsumption))==1
            assert db.scalar(select(func.count()).select_from(AttendanceEvidenceOccurrenceClaim))==2
            assert financial==tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem))
        monkeypatch.setattr(settings,'attendance_synthetic_review_enabled',False)
        assert client.post(root+'/confirm',data=confirm).status_code==404
        monkeypatch.setattr(settings,'attendance_synthetic_review_enabled',True)
        with SessionLocal.begin() as db:
            account=db.get(AdminAccount,original_actor);account.session_version+=1
        assert client.post(root+'/confirm',data=confirm).status_code==401
        client.cookies.clear()
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username='synthetic-shift-finance',password='Synthetic-Only-Login-1004!'),follow_redirects=False).status_code==303
        assert client.post(root+'/review',data={**args,'csrf_token':token(client.get('/payroll'))}).status_code==403


def test_postclassification_operational_errors_only_no_database_mutation(monkeypatch):
    """After main standalone fixture exists; mock exact SQLSTATE at service edge."""
    from sqlalchemy.exc import OperationalError
    from types import SimpleNamespace
    import app.shift_day_routes as routes
    monkeypatch.setattr(settings,'attendance_synthetic_review_enabled',True);monkeypatch.setattr(settings,'app_env','test')
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None);monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    with SessionLocal.begin() as db:
        actor=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-shift-system_admin'))
        employee=db.scalar(select(AttendanceDailyRecord.employee_id))
    with TestClient(main.app) as client:
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username='synthetic-shift-system_admin',password='Synthetic-Only-Login-1004!'),follow_redirects=False).status_code==303
        root=f'/attendance/shift-evidence/{"d"*64}/{employee}'
        csrf=token(client.get(root))
        for code,status in (('55P03',503),('08006',400)):
            def fail(db,**kwargs):raise OperationalError('synthetic',{},SimpleNamespace(sqlstate=code))
            monkeypatch.setattr(routes,'consume_evidence_review',fail)
            response=client.post(root+'/confirm',data=dict(csrf_token=csrf,receipt_id='f'*64,request_digest='0'*64,human_confirmed='yes'))
            assert response.status_code==status
            assert (response.headers.get('retry-after')=='3')==(code=='55P03')
