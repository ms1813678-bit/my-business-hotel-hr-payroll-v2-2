"""Daily OT error and state coverage, isolated follow-up candidate."""
import re
from decimal import Decimal
from datetime import date,datetime,UTC
from types import SimpleNamespace as NS
import pytest
from starlette.requests import Request
from fastapi import HTTPException

@pytest.mark.parametrize('language',['en','zh'])
@pytest.mark.parametrize('state',['correction','monthly','stale','closed','closed_missing'])
def test_actual_translator_daily_states(language,state):
    from app.main import templates
    from app.overtime_day_routes import overtime_day_router
    from app.i18n import translate_html
    overtime_day_router(templates)
    request=Request({'type':'http','method':'GET','path':'/','headers':[],'query_string':b'','session':{},'state':{'language':language,'admin_role':'system_admin'}})
    item=NS(id=1,employee_name_snapshot='Synthetic Employee',employee_code_snapshot='SYN',salary_basis=6000,performance_bonus=1000)
    run=NS(id=1,period_start=date(2026,2,1),period_end=date(2026,2,28),status='closed' if state.startswith('closed') else 'draft')
    award=NS(work_date=date(2026,2,2),manual_hours=2,rate_divisor=9,amount_unrounded=Decimal('51.85185'),receipt_id='a'*64)
    review=dict(id='b'*64,proposal=dict(work_date='2026-02-02',manual_hours=2,rate_divisor=9,reason='Operator input unchanged',predecessor_id='a'*64),extra=2,amount=Decimal('51.85185'),expires=datetime.now(UTC)) if state=='correction' else None
    monthly=NS(hours=2,amount=Decimal('51.85'),digest='c'*64) if state in ('monthly','closed') else None
    html=templates.env.get_template('overtime_day_review.html').render(request=request,item=item,run=run,awards=[award],review=review,monthly=monthly,error=state=='stale',csrf_token='synthetic',admin='synthetic')
    out=translate_html(html,language)
    assert not re.search('[\u0600-\u06ff]',out),re.findall('[\u0600-\u06ff][^<]*',out)
    warning={'en':'Correct days whose source changed before updating the monthly total.','zh':'更新月度合计前，必须更正来源已变更的天数。'}[language]
    closed_warning={'en':'The current daily decisions could not be verified against their sources. This payroll run is read-only here; daily corrections and monthly updates are not available on this page.','zh':'无法根据来源核验当前每日决定。此工资单在此处仅供查看；本页不能更正每日记录或更新月度合计。'}[language]
    assert (warning in out)==(monthly is None and not state.startswith('closed'))
    assert (closed_warning in out)==(state=='closed_missing')
    assert {'en':'Hours','zh':'时数'}[language] in out
    assert {'en':'Display amount','zh':'展示金额'}[language] in out
    assert {'en':'Daily hours rate basis','zh':'计算时薪的每日小时基数'}[language] in out
    if monthly and run.status=='draft':
        assert {'en':'Amount','zh':'金额'}[language] in out
    if state.startswith('closed'):
        assert '/overtime-days/review"' not in out and '/overtime-days/monthly"' not in out
    if review:
        review['proposal']['reason']='سبب تجريبي لا يترجم'
        html=templates.env.get_template('overtime_day_review.html').render(request=request,item=item,run=run,awards=[award],review=review,monthly=monthly,csrf_token='synthetic',admin='synthetic')
        assert review['proposal']['reason'] in translate_html(html,language)

@pytest.mark.parametrize('language',['en','zh'])
def test_product_exception_handler_overtime_unavailable_localized(language):
    import asyncio
    from app.main import authentication_exception_handler
    request=Request({'type':'http','method':'GET','path':'/payroll/items/999/overtime-days','headers':[(b'accept',b'application/json')],'query_string':b'','state':{'language':language}})
    response=asyncio.run(authentication_exception_handler(request,HTTPException(status_code=404,detail='تعذر عرض مراجعة الإضافي.')))
    assert response.status_code==404
    assert {'en':'Could not display the overtime review.','zh':'无法显示加班复核。'}[language] in response.body.decode()
    assert not re.search('[\u0600-\u06ff]',response.body.decode())


from tests.test_overtime_day_routes import daily_client, form
from tests.test_overtime_day_concurrency import committed_day

@pytest.mark.parametrize('closed',[False,True])
@pytest.mark.parametrize('missing_proof',[False,True])
def test_actual_route_monthly_proof_controls_warning(daily_client,closed,missing_proof):
    from app.models import PayrollItem,PayrollRun,AttendanceDailyRecord
    from app.i18n import translate_html
    client,Session,auth,item_id=daily_client
    url=f'/payroll/items/{item_id}/overtime-days'
    reviewed=client.post(url+'/review',data=form())
    receipt=re.search(r'name="receipt_id" value="([a-f0-9]{64})"',reviewed.text).group(1)
    assert client.post(url+'/confirm',data=dict(csrf_token='synthetic-csrf-only',receipt_id=receipt,human_confirmed='yes')).status_code==200
    with Session.begin() as db:
        item=db.get(PayrollItem,item_id)
        if closed:db.get(PayrollRun,item.payroll_run_id).status='closed'
        if missing_proof:
            from sqlalchemy import select
            row=db.scalar(select(AttendanceDailyRecord).where(AttendanceDailyRecord.employee_id==item.employee_id))
            row.review_notes='Synthetic source changed after award'
    page=client.get(url)
    assert page.status_code==200
    assert (page.context['monthly'] is None)==missing_proof
    for language,warning in [('en','Correct days whose source changed before updating the monthly total.'),('zh','更新月度合计前，必须更正来源已变更的天数。')]:
        out=translate_html(page.text,language)
        closed_warning={'en':'The current daily decisions could not be verified against their sources. This payroll run is read-only here; daily corrections and monthly updates are not available on this page.','zh':'无法根据来源核验当前每日决定。此工资单在此处仅供查看；本页不能更正每日记录或更新月度合计。'}[language]
        assert (warning in out)==(missing_proof and not closed)
        assert (closed_warning in out)==(missing_proof and closed)
        assert ('/overtime-days/monthly"' in out)==(not missing_proof and not closed)
        if closed:assert '/overtime-days/review"' not in out


def test_actual_route_missing_item_error_matches_catalog(daily_client):
    from app.i18n import EN,ZH
    client,Session,auth,item_id=daily_client
    response=client.get('/payroll/items/999999999/overtime-days')
    assert response.status_code==404
    detail=response.json()['detail']
    assert EN[detail]=='Could not display the overtime review.'
    assert ZH[detail]=='无法显示加班复核。'
