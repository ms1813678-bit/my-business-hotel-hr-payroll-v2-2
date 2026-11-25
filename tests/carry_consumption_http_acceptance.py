from datetime import date
import os
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app import main
from app.db import SessionLocal
from app.models import AdminAccount,LeaveBalanceTransaction,LeaveCarryConsumption
from app.security import hash_password
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_leave_carry_consumption import ready
from tests.test_leave_next_year_earning import FULL
from tests.test_shift_day_http import token

@pytest.mark.parametrize('prior_year',[{'hire_date':FULL,'used_annual':6}],indirect=True)
def test_actual_review_confirm_and_idempotent_http(prior_year,monkeypatch):
    editor_role=os.environ.get('HR_CARRY_EDITOR_ROLE','system_admin')
    db,call=ready(prior_year,category=os.environ.get('HR_CARRY_CATEGORY','W'))
    actor=db.get(AdminAccount,call['actor_id']);actor.password_hash=hash_password('Synthetic-Carry-Only-1987!')
    if editor_role=='assistant_manager':
        actor=AdminAccount(username='synthetic-carry-assistant',role=editor_role,password_hash=hash_password('Synthetic-Carry-Only-1987!'))
        db.add(actor);db.flush()
    username=actor.username;employee_id=call['employee_id']
    language=os.environ.get('HR_CARRY_LANGUAGE','ar')
    prior_year[1].full_name={'ar':'موظف تجريبي','en':'Test employee','zh':'测试员工'}[language]
    count=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction));db.commit()
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    root=f'/leave/carry-consumption/{employee_id}'
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',language)
        assert client.get(root).status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=username,
            password='Synthetic-Carry-Only-1987!'),follow_redirects=False).status_code==303
        page=client.get(root);assert page.status_code==200,page.text[:300]
        assert {'ar':'تخصيص استعمال الترحيل','en':'Allocate carry usage','zh':'分配结转使用'}[language] in page.text
        assert root in client.get('/leave/workbench',params={'employee_id':employee_id}).text
        if editor_role=='assistant_manager':
            assert client.get('/leave/cash-out/'+str(employee_id)).status_code==403
            assert client.get('/leave/annual-settlement/'+prior_year[3]['selection_id']).status_code==403
        data=dict(csrf_token=token(page),grant=call['grant_kind']+':'+call['grant_id'],
            usage=call['monthly_receipt_id']+':'+str(call['usage_ledger_id']),work_date=call['work_date'].isoformat(),
            quantity='2/7',reason={'ar':'مراجعة استعمال تجريبي','en':'Test usage review','zh':'测试使用审核'}[language])
        assert client.post(root+'/review',data={k:v for k,v in data.items() if k!='csrf_token'}).status_code==403
        for change in [dict(quantity='0'),dict(quantity='8/7'),dict(work_date='2026-01-04'),dict(grant='carry:'+'f'*64),dict(extra='bad')]:
            assert client.post(root+'/review',data={**data,**change}).status_code==400
        reviewed=client.post(root+'/review',data=data);assert reviewed.status_code==200,reviewed.text[:500]
        assert 'id="carry-review"' in reviewed.text
        proof=re.search('name="proof" value="([^"]+)"',reviewed.text).group(1)
        confirm=dict(csrf_token=token(reviewed),proof=proof,human_confirmed='yes')
        assert client.post(root+'/confirm',data={**confirm,'proof':proof+'x'}).status_code==400
        assert client.post(root+'/confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        assert client.post(root+'/confirm',data={**confirm,'extra':'bad'}).status_code==400
        from app.models import LeaveRegisterEntry
        with SessionLocal() as change:
            entry=change.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,LeaveRegisterEntry.work_date==call['work_date']))
            original=entry.code;entry.code='U';change.commit()
        assert client.post(root+'/confirm',data=confirm).status_code==400
        with SessionLocal() as change:
            entry=change.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==employee_id,LeaveRegisterEntry.work_date==call['work_date']))
            entry.code=original;change.commit()
        saved=client.post(root+'/confirm',data=confirm);assert saved.status_code==200,saved.text[:500]
        assert 'id="carry-saved"' in saved.text
        assert client.post(root+'/confirm',data=confirm).status_code==200
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(LeaveCarryConsumption))==1
            assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==count
        if editor_role=='system_admin' and os.environ.get('HR_PIXEL_DIR'):
            out=Path(os.environ['HR_PIXEL_DIR']);out.mkdir(parents=True,exist_ok=True)
            for name,response in [('form',page),('review',reviewed),('saved',saved)]:
                (out/(name+'.html')).write_text(response.text)

        with SessionLocal() as change:
            current=change.scalar(select(AdminAccount).where(AdminAccount.username==username))
            current.role='finance';change.commit()
        assert client.get(root).status_code==403
        assert client.post(root+'/review',data=data).status_code==403
        assert client.post(root+'/confirm',data=confirm).status_code==403
