"""Actual normal database HTTP earning, authenticated HR, CSRF, flags disabled."""
import os,re,json,base64
from datetime import date
from fractions import Fraction
from decimal import Decimal
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from itsdangerous import TimestampSigner
from app import main
from app.config import settings
from app.db import SessionLocal
from app.security import hash_password
from app.models import (LeaveOpeningSelection,LeaveType,Employee,AdminAccount,LeaveBalanceTransaction,LeaveMonthEarning)
from app.leave_opening_apply import review_opening_apply,apply_confirmed_opening
from app.leave_return_register import preview_return_register,consume_return_register
from app.leave_workflow import leave_balance
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_return_register_cycle import setup_return_chain
from tests.test_shift_day_http import token


def test_real_http_exact_month_post_review_identity_csrf_and_locales(history_context,monkeypatch):
    lang=os.environ['MONTH_EARNING_ACCEPTANCE_LANG']
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    db,actor,auth,fact=history_context
    actor.password_hash=hash_password('Synthetic-Earning-Only-1500!')
    db.flush()
    ctx=setup_return_chain(history_context,multi=True)
    employee=ctx['employee'];comparison=ctx['compared']
    proposed,digest=preview_return_register(db,comparison_id=comparison.id,employee_id=employee.id,**auth)
    consume_return_register(db,operation_id='7'*64,comparison_id=comparison.id,employee_id=employee.id,reviewed_digest=digest,human_confirmed=True,reason='Synthetic actual reviewed register',**auth)
    selected=db.scalar(select(LeaveOpeningSelection).where(LeaveOpeningSelection.employee_id==employee.id))
    kinds={c:db.scalar(select(LeaveType.id).where(LeaveType.code==host)) for c,host in {'A':'annual','W':'weekly_rest','H':'official_holiday'}.items()}
    opening=review_opening_apply(db,selection_id=selected.id,type_map=kinds,**auth)
    apply_confirmed_opening(db,selection_id=selected.id,type_map=kinds,reviewed_apply_sha256=opening['apply_sha256'],human_confirmed=True,reason='Synthetic actual opening',**auth)
    for role in ('assistant_manager','finance'):
        db.add(AdminAccount(username='synthetic-month-'+role,password_hash=hash_password('Synthetic-Earning-Only-1500!'),role=role,active=True))
    db.flush();db.commit()
    assert not settings.leave_synthetic_register_enabled and not settings.attendance_synthetic_review_enabled
    assert not settings.leave_synthetic_transition_enabled and not main.app.dependency_overrides
    url=f'/leave/month-review/earning/{comparison.id}/{employee.id}'
    capture=Path(os.environ['MONTH_EARNING_CAPTURE_DIR']);capture.mkdir(parents=True,exist_ok=True)
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',lang)
        assert client.get(url).status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=actor.username,password='Synthetic-Earning-Only-1500!'),follow_redirects=False).status_code==303
        page=client.get(url)
        assert page.status_code==200 and page.headers['cache-control']=='no-store'
        (capture/(lang+'-form.html')).write_text(page.text,encoding='utf-8')
        data=dict(csrf_token=token(page),evaluated_on='2026-05-31',reason='Synthetic explicit monthly review',mode='earning')
        assert client.post(url+'/review',data={k:v for k,v in data.items() if k!='csrf_token'}).status_code==403
        assert client.post(url+'/review',data=data|{'extra':'1'}).status_code==400
        reviewed=client.post(url+'/review',data=data)
        assert reviewed.status_code==200 and 'id="earning-review"' in reviewed.text
        (capture/(lang+'-review.html')).write_text(reviewed.text,encoding='utf-8')
        content=reviewed.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
        if lang!='ar':assert re.search(r'[\u0600-\u06ff]',re.sub(r'<bdi>.*?</bdi>','',content)) is None
        proof=re.search('name="proof" value="([^"]+)"',reviewed.text).group(1)
        confirm=dict(csrf_token=token(reviewed),proof=proof,human_confirmed='yes')
        assert client.post(url+'/confirm',data=confirm|{'proof':proof+'x'}).status_code==400
        assert client.post(url+'/confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        from concurrent.futures import ThreadPoolExecutor
        def concurrent_confirm(_):
            with TestClient(main.app) as parallel:
                parallel.cookies.update(client.cookies)
                return parallel.post(url+'/confirm',data=confirm)
        # Hold the FIRST real exclusive transaction after its actual guard.
        # The second authenticated HTTP request is issued only after that lock
        # exists. No guard, role or middleware is bypassed.
        from threading import Event, Lock
        import app.leave_month_earning_routes as routes
        acquired,finish=Event(),Event()
        once=Lock()
        real_guard=routes._guard
        def held_guard(*args,**kwargs):
            result=real_guard(*args,**kwargs)
            if once.acquire(blocking=False):
                acquired.set()
                assert finish.wait(15), 'first transaction overlap release timeout'
            return result
        with monkeypatch.context() as overlap:
            overlap.setattr(routes,'_guard',held_guard)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(concurrent_confirm,0)
                try:
                    assert acquired.wait(10), 'first HTTP transaction never held real exclusive guard'
                    second=pool.submit(concurrent_confirm,1)
                    busy=second.result(timeout=10)
                    assert busy.status_code==503 and busy.headers.get('Retry-After'),(busy.status_code,busy.text[:250])
                    assert not first.done(), 'first transaction must still hold exclusive gate'
                    with SessionLocal() as check:
                        assert check.scalar(select(func.count()).select_from(LeaveMonthEarning))==0
                finally:
                    finish.set()
                saved=first.result(timeout=10)
        assert client.post(url+'/confirm',data=confirm).status_code==200
        assert saved.status_code==200 and 'id="earning-saved"' in saved.text
        (capture/(lang+'-saved.html')).write_text(saved.text,encoding='utf-8')
        if lang!='ar':
            saved_content=saved.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',re.sub(r'<bdi>.*?</bdi>','',saved_content)) is None
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(LeaveMonthEarning))==1
            assert leave_balance(check,employee.id,kinds['A'])==Decimal(7) # opening8, Ause1, heldannualnotcredited
            assert leave_balance(check,employee.id,kinds['W'])==Fraction(16,7) # opening2 +2/7
            count=check.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
        assert client.post(url+'/confirm',data=confirm).status_code==200
        with SessionLocal() as check:assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==count
        release_review=client.post(url+'/review',data=dict(csrf_token=token(saved),evaluated_on='2026-07-01',reason='Synthetic first full June completed',mode='release'))
        assert release_review.status_code==200 and 'id="earning-review"' in release_review.text
        (capture/(lang+'-release-review.html')).write_text(release_review.text,encoding='utf-8')
        release_proof=re.search('name="proof" value="([^"]+)"',release_review.text).group(1)
        release_data=dict(csrf_token=token(release_review),proof=release_proof,human_confirmed='yes')
        released=client.post(url+'/confirm',data=release_data)
        assert released.status_code==200 and 'id="earning-saved"' in released.text
        assert client.post(url+'/confirm',data=release_data).status_code==200
        with SessionLocal() as check:
            assert leave_balance(check,employee.id,kinds['A'])==Fraction(439,62)
            assert leave_balance(check,employee.id,kinds['W'])==Fraction(16,7)
            assert check.scalar(select(func.count()).select_from(LeaveMonthEarning))==1
        for role in ('assistant_manager','finance'):
            with TestClient(main.app) as other:
                assert other.post('/login',data=dict(csrf_token=token(other.get('/login')),username='synthetic-month-'+role,password='Synthetic-Earning-Only-1500!'),follow_redirects=False).status_code==303
                assert other.get(url).status_code==403
                assert other.post(url+'/confirm',data=confirm).status_code in (403,400)
