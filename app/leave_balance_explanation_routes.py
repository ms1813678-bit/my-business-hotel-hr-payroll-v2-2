# ruff: noqa: B008
"""HR read-only release/ledger balance explanation, never current credit approval."""
from datetime import date
from decimal import Decimal
import re
from fastapi import APIRouter,Depends,Request,HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.leave_balance_explanation import explain_selected_balance
from app.leave_opening_source import read_confirmed_opening_source
from app.models import AdminAccount,Employee


def balance_explanation_router(templates):
    router=APIRouter()
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def day(raw):
        if not isinstance(raw,str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',raw) is None:raise ValueError('Canonical day required.')
        value=date.fromisoformat(raw)
        if value.isoformat()!=raw:raise ValueError('Canonical day required.')
        return value
    def target_month(raw):
        if not isinstance(raw,str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}',raw) is None:raise ValueError('Canonical month required.')
        return day(raw+'-01')
    def source(db,request,admin,selection_id):
        if re.fullmatch('[0-9a-f]{64}',selection_id) is None:raise ValueError('Exact selected source required.')
        reviews,_,_=read_confirmed_opening_source(db,selection_id=selection_id,**identity(request,admin))
        employee=db.get(Employee,reviews[0]['employee_id'],populate_existing=True)
        if employee is None:raise ValueError('Employee source missing.')
        return employee
    def render(db,request,admin,selection_id,month,result=None,submitted=None):
        employee=source(db,request,admin,selection_id)
        target=target_month(month) if month else None
        months=[date(target.year,m,1) for m in range(1,target.month+1)] if target else []
        response=templates.TemplateResponse(request=request,name='leave_balance_explanation.html',context=dict(
            admin=admin.username,csrf_token=get_csrf_token(request),selection_id=selection_id,month=month,months=months,
            employee=employee,result=result,submitted=submitted or {},issue_labels={
                'cancelled_record_still_linked_in_register':'السجل ملغى، لكن أيامه ما زالت مرتبطة بسجل الأيام.',
                'fractional_charge_cannot_map_to_whole_register_days':'الخصم كسري ولا يطابق عدد أيام كاملة دون توزيع صريح.',
                'record_not_confirmed':'السجل المرتبط غير مؤكد.',
                'invalid_nonpositive_or_nonfinite_record_charge':'كمية السجل غير صالحة؛ بقيت ظاهرة للمراجعة.',
                'register_record_identity_or_code_mismatch':'هوية الموظف أو نوع اليوم لا يطابق السجل المرتبط.',
                'ledger_record_identity_mismatch':'هوية حركة الدفتر لا تطابق السجل المرتبط.',
                'linked_register_coverage_incomplete_or_conflicting':'تغطية الأيام المرتبطة ناقصة أو متعارضة.',
                'linked_use_amount_or_count_disagrees':'عدد حركات الاستعمال أو كميتها لا يطابق السجل.',
                'record_crosses_comparison_window_no_allocation':'السجل يتجاوز فترة المقارنة؛ لم يُوزع على الفترة تلقائيًا.',
                'record_type_not_balance_tracked':'نوع السجل لا يتتبع رصيدًا.',
                'ambiguous_canonical_type_mapping':'ربط نوع الإجازة بتصنيف الرصيد غير فريد.',
                'record_category_unresolved':'تصنيف رصيد السجل غير محسوم.',
                'record_date_span_requires_separate_review':'مدة السجل تتجاوز نطاق المراجعة المحلية.',
                'charged_calendar_days_disagree':'الأيام المخصومة والتقويمية غير متطابقة.',
                'unexpected_linked_ledger_transaction':'توجد حركة مرتبطة من نوع غير متوقع.',
                'confirmed_record_has_reversal':'السجل مؤكد وله حركة عكسية مرتبطة.',
                'cancelled_record_reversal_disagrees':'الحركة العكسية للسجل الملغى ناقصة أو مختلفة.'}))
        response.headers['Cache-Control']='no-store'
        return response
    @router.get('/leave/balance-explanation')
    def page(request:Request,selection_id:str,month:str='',admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:return render(db,request,admin,selection_id,month)
        except (ValueError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='تعذر عرض تفسير الرصيد. راجع المصدر الحالي والفترة والجلسة.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/leave/balance-explanation',dependencies=[Depends(csrf_protect)])
    async def preview(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            fixed={'csrf_token','selection_id','month','release_evaluated_on'}
            if not fixed<=set(form):raise ValueError('Complete evidence required.')
            selection_id=str(form['selection_id']);month=str(form['month']);target=target_month(month)
            months=[date(target.year,m,1) for m in range(1,target.month+1)]
            ages={m:day(str(form['age_'+m.isoformat()])) for m in months}
            with SessionLocal() as db:
                employee=source(db,request,admin,selection_id)
                insurance={};expected=fixed|{'age_'+m.isoformat() for m in months}
                for m in months:
                    key='insurance_'+m.isoformat();expected.add(key);raw=str(form.get(key,''))
                    if raw and re.fullmatch(r'[0-9]{1,60}(?:\.[0-9]{1,30})?',raw) is None:raise ValueError('Bounded explicit insurance required.')
                    insurance[employee.id,m]=Decimal(raw) if raw else None
                if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected):raise ValueError('Exact unique evidence required.')
                result=explain_selected_balance(db,selection_ids=(selection_id,),department_id=employee.department_id,month_start=target,
                    current_insurance_years=insurance,age_evaluation_dates=ages,release_evaluated_on=day(str(form['release_evaluated_on'])),**identity(request,admin))
                response=render(db,request,admin,selection_id,month,result,dict(form))
                db.rollback()
            return response
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return RedirectResponse('/leave/departments?error=1',status_code=303,headers={'Cache-Control':'no-store'})
    return router
