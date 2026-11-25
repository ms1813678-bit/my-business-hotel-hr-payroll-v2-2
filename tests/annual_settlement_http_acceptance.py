from datetime import date
import re
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app import main
from app.security import hash_password
from app.db import SessionLocal
from app.models import LeaveAnnualSettlement,LeaveCashOut
from app.leave_workflow import leave_balance
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_cash_out import payroll
from tests.test_shift_day_http import token


@pytest.mark.parametrize('prior_year',[{'hire_date':date(2025,1,1),'used_annual':6}],indirect=True)
def test_real_annual_http_confirmed_cash_and_carry(prior_year,monkeypatch):
    from pathlib import Path
    db,employee,kinds,args=prior_year
    actor=db.get(__import__('app.models',fromlist=['AdminAccount']).AdminAccount,args['actor_id'])
    import os
    lang=os.environ.get('HR_ANNUAL_LANGUAGE','ar')
    label={'ar':'موظف تجريبي','en':'Test employee','zh':'测试员工'}[lang]
    reason={'ar':'مراجعة سنوية تجريبية','en':'Test annual review','zh':'年度审核测试'}[lang]
    employee.full_name=label
    actor.password_hash=hash_password('Synthetic-Annual-Only-1500!');db.flush()
    run,item=payroll(db,employee,start=date(2025,12,1),end=date(2025,12,31),status='draft')
    employee_id=employee.id;type_id=kinds['A'];item_id=item.id;username=actor.username;db.commit()
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    root='/leave/annual-settlement/'+args['selection_id']
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language',lang)
        assert client.get(root).status_code in (401,403)
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username=username,
            password='Synthetic-Annual-Only-1500!'),follow_redirects=False).status_code==303
        page=client.get(root);assert page.status_code==200
        assert page.headers['content-language']=={'ar':'ar','en':'en','zh':'zh-CN'}[lang]
        assert {'ar':'التسوية السنوية','en':'Annual settlement','zh':'年度结算'}[lang] in page.text
        work=client.get('/leave/workbench',params={'employee_id':employee_id})
        assert work.status_code==200 and root in work.text

        data=dict(csrf_token=token(page),reviewed_on='2026-01-01',payroll_item_id=str(item_id),
            available_from='2026-01-01',available_through='2026-04-30',reason=reason)
        assert client.post(root+'/review',data={k:v for k,v in data.items() if k!='csrf_token'}).status_code==403
        reviewed=client.post(root+'/review',data=data)
        assert reviewed.status_code==200,reviewed.text[:250]
        assert 'id="annual-review"' in reviewed.text and '7.50' in reviewed.text and '1.50' in reviewed.text
        proof=re.search('name="proof" value="([^"]+)"',reviewed.text).group(1)
        confirm=dict(csrf_token=token(reviewed),proof=proof,human_confirmed='yes')
        assert client.post(root+'/confirm',data={**confirm,'proof':proof+'x'}).status_code==400
        assert client.post(root+'/confirm',data={k:v for k,v in confirm.items() if k!='human_confirmed'}).status_code==400
        assert client.post(root+'/confirm',data={**confirm,'human_confirmed':'no'}).status_code==400
        assert client.post(root+'/confirm',data={**confirm,'extra':'bad'}).status_code==400
        assert client.post(root+'/review',data={**data,'payroll_item_id':'-1'}).status_code==400
        saved=client.post(root+'/confirm',data=confirm)
        assert saved.status_code==200,saved.text[:250]
        assert 'id="annual-saved"' in saved.text
        assert 'name="payroll_item_id"' not in saved.text
        assert client.post(root+'/review',data=data).status_code==400
        assert client.post(root+'/confirm',data=confirm).status_code==200
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(LeaveAnnualSettlement))==1
            assert check.scalar(select(func.count()).select_from(LeaveCashOut))==1
            assert leave_balance(check,employee_id,type_id)==__import__('decimal').Decimal('1.5')
        import os
        if os.environ.get('HR_PIXEL_DIR'):
            out=Path(os.environ['HR_PIXEL_DIR']);out.mkdir(parents=True,exist_ok=True)
            for name,response in [('form',page),('review',reviewed),('saved',saved)]:
                (out/(name+'.html')).write_text(response.text)
