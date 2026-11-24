# ruff: noqa: B008
"""HR-only saved pay-basis what-if page, no financial commit action."""
from decimal import Decimal,localcontext
import re
from fastapi import APIRouter,Depends,Request,HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.models import AdminAccount
from app.pay_basis_source import preview_stored_pay_basis


def pay_basis_router(templates):
    router=APIRouter()
    def display_amount(value):
        with localcontext() as context:
            context.prec=500
            return format(value,'.2f')
    templates.env.filters['pay_preview_amount']=display_amount
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def amount(raw):
        if not isinstance(raw,str) or re.fullmatch(r'[0-9]{1,30}(?:\.[0-9]{1,20})?',raw) is None:raise ValueError('Explicit bounded decimal required.')
        return Decimal(raw)
    def render(request,admin,result,submitted):
        response=templates.TemplateResponse(request=request,name='pay_basis_preview.html',context=dict(admin=admin.username,csrf_token=get_csrf_token(request),comparison=result,submitted=submitted))
        response.headers['Cache-Control']='no-store';return response
    @router.get('/payroll/items/{item_id}/basis-preview')
    def page(request:Request,item_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                result=preview_stored_pay_basis(db,item_id=item_id,cashout_days=Decimal(0),manual_overtime_hours=Decimal(0),overtime_divisor_hours=8,**identity(request,admin))
                return render(request,admin,result,{})
        except (ValueError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='تعذر عرض معاينة أساس الأجر المحفوظ.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/payroll/items/{item_id}/basis-preview',dependencies=[Depends(csrf_protect)])
    async def preview(request:Request,item_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            expected={'csrf_token','cashout_days','manual_overtime_hours','overtime_divisor_hours'}
            if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected):raise ValueError('Exact unique preview fields required.')
            divisor=str(form['overtime_divisor_hours'])
            if divisor not in ('8','9','12'):raise ValueError('Explicit divisor required.')
            with SessionLocal() as db:
                result=preview_stored_pay_basis(db,item_id=item_id,cashout_days=amount(str(form['cashout_days'])),manual_overtime_hours=amount(str(form['manual_overtime_hours'])),overtime_divisor_hours=int(divisor),**identity(request,admin))
                response=render(request,admin,result,dict(form));db.rollback();return response
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return RedirectResponse('/payroll?error=item',status_code=303,headers={'Cache-Control':'no-store'})
    @router.get('/payroll/items/{item_id}/overtime-day-preview')
    def overtime_page(request:Request,item_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                payroll=preview_stored_pay_basis(db,item_id=item_id,cashout_days=Decimal(0),manual_overtime_hours=Decimal(0),overtime_divisor_hours=8,**identity(request,admin))
                response=templates.TemplateResponse(request=request,name='overtime_day_preview.html',context=dict(admin=admin.username,csrf_token=get_csrf_token(request),payroll=payroll,result=None,submitted={}))
                response.headers['Cache-Control']='no-store';return response
        except (ValueError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='Saved payroll item unavailable.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/payroll/items/{item_id}/overtime-day-preview',dependencies=[Depends(csrf_protect)])
    async def overtime_preview(request:Request,item_id:int,admin:AdminAccount=Depends(require_system_admin)):
        from datetime import date
        from app.overtime_day_source import preview_stored_overtime_day
        try:
            form=await request.form()
            expected={'csrf_token','work_date','manual_hours','rate_divisor_hours','override_reason'}
            if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected):raise ValueError('Exact unique preview fields required.')
            day=str(form['work_date'])
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',day) is None:raise ValueError('Explicit date required.')
            divisor=str(form['rate_divisor_hours'])
            if divisor not in ('8','9','12'):raise ValueError('Explicit divisor required.')
            with SessionLocal() as db:
                result=preview_stored_overtime_day(db,item_id=item_id,work_date=date.fromisoformat(day),manual_hours=amount(str(form['manual_hours'])),rate_divisor_hours=int(divisor),override_reason=str(form['override_reason']),**identity(request,admin))
                response=templates.TemplateResponse(request=request,name='overtime_day_preview.html',context=dict(admin=admin.username,csrf_token=get_csrf_token(request),payroll=result.payroll,result=result,submitted=dict(form)))
                response.headers['Cache-Control']='no-store';db.rollback();return response
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return RedirectResponse('/payroll?error=item',status_code=303,headers={'Cache-Control':'no-store'})
    return router
