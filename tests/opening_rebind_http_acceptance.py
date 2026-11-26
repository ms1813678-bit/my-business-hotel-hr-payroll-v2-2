from datetime import date
from decimal import Decimal
import os
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app import main
from app.db import SessionLocal
from app.models import AdminAccount,LeaveBalanceTransaction,AuditLog
from app.security import hash_password
from tests.test_department_workbook_store import context
from tests.test_leave_historical_evidence import history_context
from tests.test_leave_completed_year_source import prior_year
from tests.test_restore_epoch_rebind import world,restore,confirm as confirm_source
from tests.test_leave_next_year_earning import FULL
from tests.test_shift_day_http import token

def test_actual_rebind_review_confirmation_and_replay(world,monkeypatch):
    language=os.environ.get('HR_REBIND_LANGUAGE','ar')
    db,employee,kinds,old,auth,args=world
    auth2=restore(db,auth,'HTTP-restored')
    new=confirm_source(db,employee,auth2,{'A':Decimal('3.5'),'W':Decimal(1),'H':Decimal(0)})
    call={**auth2,'employee_id':employee.id,'old_selection_id':old.id,'new_selection_id':new.id}
    actor=db.get(AdminAccount,call['actor_id']);actor.password_hash=hash_password('Synthetic-Rebind-Only-2025!')
    username=actor.username;employee_id=call['employee_id']
    employee.full_name={'ar':'موظف تجريبي','en':'Test employee','zh':'测试员工'}[language]
    count=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction));db.commit()
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    root=f'/leave/opening-rebind/{employee_id}'
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',language)
        assert client.get(root).status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=username,
            password='Synthetic-Rebind-Only-2025!'),follow_redirects=False).status_code==303
        page=client.get(root);assert page.status_code==200,page.text[:500]
        assert {'ar':'إعادة ربط المصدر بعد الاستعادة','en':'Rebind source after restore','zh':'恢复后重新关联来源'}[language] in page.text
        assert root in client.get('/leave/workbench',params={'employee_id':employee_id}).text
        data=dict(csrf_token=token(page),old_selection=old.id,new_selection=new.id,
            reason={'ar':'إعادة ربط مصدر تجريبي','en':'Rebind test source','zh':'重新关联测试来源'}[language])
        assert client.post(root+'/review',data={k:v for k,v in data.items() if k!='csrf_token'}).status_code==403
        for change in [dict(new_selection=old.id),dict(old_selection='a'*64),dict(extra='bad'),dict(reason=' ')]:
            assert client.post(root+'/review',data={**data,**change}).status_code==400
        reviewed=client.post(root+'/review',data=data);assert reviewed.status_code==200,reviewed.text[:500]
        assert 'id="rebind-review"' in reviewed.text
        proof=re.search('name="proof" value="([^"]+)"',reviewed.text).group(1)
        confirm=dict(csrf_token=token(reviewed),proof=proof,human_confirmed='yes')
        assert client.post(root+'/confirm',data={**confirm,'proof':proof+'x'}).status_code==400
        assert client.post(root+'/confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        assert client.post(root+'/confirm',data={**confirm,'extra':'bad'}).status_code==400
        saved=client.post(root+'/confirm',data=confirm);assert saved.status_code==200,saved.text[:500]
        assert 'id="rebind-saved"' in saved.text
        assert client.post(root+'/confirm',data=confirm).status_code==200
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='leave_opening_rebound'))==1
            assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==count
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
