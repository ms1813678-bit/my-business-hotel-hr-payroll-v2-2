from datetime import date
import os
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app import main
from app.db import SessionLocal
from app.models import AdminAccount,LeaveBalanceTransaction,LeaveCarryExpiry
from app.security import hash_password
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_carry_expiry import prepared_expiry
from tests.test_leave_next_year_earning import FULL
from tests.test_shift_day_http import token

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_actual_expiry_review_confirmation_and_replay(prior_year,monkeypatch):
    language=os.environ.get('HR_EXPIRY_LANGUAGE','ar')
    db,call,usage,review=prepared_expiry(prior_year,os.environ.get('HR_EXPIRY_CATEGORY','A'))
    actor=db.get(AdminAccount,call['actor_id']);actor.password_hash=hash_password('Synthetic-Expiry-Only-2010!')
    username=actor.username;employee_id=call['employee_id']
    prior_year[1].full_name={'ar':'موظف تجريبي','en':'Test employee','zh':'测试员工'}[language]
    count=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction));db.commit()
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    root=f'/leave/carry-expiry/{employee_id}'
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',language)
        assert client.get(root).status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=username,
            password='Synthetic-Expiry-Only-2010!'),follow_redirects=False).status_code==303
        page=client.get(root);assert page.status_code==200,page.text[:500]
        assert {'ar':'استبعاد الترحيل المنتهي','en':'Exclude expired carry','zh':'排除已到期结转'}[language] in page.text
        assert root in client.get('/leave/workbench',params={'employee_id':employee_id}).text
        data=dict(csrf_token=token(page),grant=call['grant_kind']+':'+call['grant_id'],
            evaluated_on='2026-05-01',reason={'ar':'استبعاد منحة تجريبية منتهية','en':'Exclude expired test grant','zh':'排除已到期测试额度'}[language])
        assert client.post(root+'/review',data={k:v for k,v in data.items() if k!='csrf_token'}).status_code==403
        for change in [dict(evaluated_on='2026-04-30'),dict(grant='carry:'+'a'*64),dict(extra='bad')]:
            assert client.post(root+'/review',data={**data,**change}).status_code==400
        reviewed=client.post(root+'/review',data=data);assert reviewed.status_code==200,reviewed.text[:500]
        assert 'id="expiry-review"' in reviewed.text
        proof=re.search('name="proof" value="([^"]+)"',reviewed.text).group(1)
        confirm=dict(csrf_token=token(reviewed),proof=proof,human_confirmed='yes')
        assert client.post(root+'/confirm',data={**confirm,'proof':proof+'x'}).status_code==400
        assert client.post(root+'/confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        assert client.post(root+'/confirm',data={**confirm,'extra':'bad'}).status_code==400
        from app.models import LeaveRegisterEntry
        with SessionLocal() as change:
            entry=change.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,
                LeaveRegisterEntry.work_date==date(2026,4,15)))
            original=entry.code;entry.code='A';change.commit()
        assert client.post(root+'/confirm',data=confirm).status_code==400
        with SessionLocal() as change:
            entry=change.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,
                LeaveRegisterEntry.work_date==date(2026,4,15)))
            entry.code=original;change.commit()
        saved=client.post(root+'/confirm',data=confirm);assert saved.status_code==200,saved.text[:500]
        assert 'id="expiry-saved"' in saved.text
        assert client.post(root+'/confirm',data=confirm).status_code==200
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(LeaveCarryExpiry))==1
            assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==count+1
        if os.environ.get('HR_PIXEL_DIR'):
            out=Path(os.environ['HR_PIXEL_DIR']);out.mkdir(parents=True,exist_ok=True)
            for name,response in [('form',page),('review',reviewed),('saved',saved)]:
                (out/(name+'.html')).write_text(response.text)
        for role in ('assistant_manager','finance'):
            with SessionLocal() as change:
                current=change.scalar(select(AdminAccount).where(AdminAccount.username==username))
                current.role=role;change.commit()
            assert client.get(root).status_code==403
            assert client.post(root+'/review',data=data).status_code==403
            assert client.post(root+'/confirm',data=confirm).status_code==403
