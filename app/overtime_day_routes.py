# ruff: noqa: B008
"""Daily manual review and append-only replacement routes."""
from datetime import date
from decimal import Decimal
from uuid import uuid4
import json,re
from fastapi import APIRouter,Depends,Request,HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_payroll_access
from app.models import AdminAccount,PayrollItem,PayrollRun
from app.overtime_day_award import (prepare_day_award,confirm_day_award,authority,lock_scope,
    own_receipt,derive,current_awards,canonical,stage_monthly_totals,verified_day_totals)


def overtime_day_router(templates):
    router=APIRouter()
    from decimal import localcontext
    def daily_display(value):
        with localcontext() as ctx:
            ctx.prec=500
            return format(Decimal(value),'.2f')
    templates.env.filters['daily_ot_display']=daily_display
    def hourly_rate(item,divisor):
        with localcontext() as ctx:
            ctx.prec=250
            return (Decimal(item.salary_basis)+Decimal(item.performance_bonus))/Decimal(30)/Decimal(divisor)
    templates.env.globals['daily_ot_rate']=hourly_rate
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def render(request,admin,item,run,awards,review=None,error=None,monthly=None):
        response=templates.TemplateResponse(request=request,name='overtime_day_review.html',context=dict(admin=admin.username,csrf_token=get_csrf_token(request),item=item,run=run,awards=awards,review=review,error=error,monthly=monthly))
        response.headers['Cache-Control']='no-store';return response
    def scope(db,item_id):
        item=db.get(PayrollItem,item_id,populate_existing=True)
        if item is None:raise ValueError('Missing item')
        run=db.get(PayrollRun,item.payroll_run_id,populate_existing=True)
        if run is None:raise ValueError('Missing run')
        return item,run
    def failure(item_id):return RedirectResponse(f'/payroll/items/{item_id}/overtime-days?error=review',status_code=303,headers={'Cache-Control':'no-store'})
    @router.get('/payroll/items/{item_id}/overtime-days')
    def page(request:Request,item_id:int,admin:AdminAccount=Depends(require_payroll_access)):
        try:
            with SessionLocal() as db:
                # The non-exempt middleware shared gate MUST already be held.
                # Otherwise the first advisory SELECT could wait on restore after
                # taking a pre-restore repeatable-read snapshot. Pin test covers it.
                # Consistent read-only snapshot. No table or credential-write locks.
                from sqlalchemy import text
                db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
                authority(db,identity(request,admin),read_only=True)
                item,run=scope(db,item_id)
                monthly=None
                try:
                    hours,amount,digest=verified_day_totals(db,item_id)
                    from app.payroll_math import money
                    monthly=dict(hours=hours,amount=money(amount),digest=digest)
                except ValueError:pass
                return render(request,admin,item,run,current_awards(db,item_id),error=request.query_params.get('error'),monthly=monthly)
        except (ValueError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='تعذر عرض مراجعة الإضافي.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/payroll/items/{item_id}/overtime-days/review',dependencies=[Depends(csrf_protect)])
    async def review(request:Request,item_id:int,admin:AdminAccount=Depends(require_payroll_access)):
        try:
            form=await request.form();expected={'csrf_token','work_date','manual_hours','rate_divisor','reason','predecessor_id'}
            if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected):raise ValueError('Exact unique fields required.')
            raw=str(form['manual_hours'])
            if re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,6})?',raw) is None:raise ValueError('Exact stored hour precision required.')
            divisor=str(form['rate_divisor'])
            if divisor not in ('8','9','12'):raise ValueError('Explicit divisor required.')
            with SessionLocal.begin() as db:
                row=prepare_day_award(db,operation_id=uuid4().hex+uuid4().hex,item_id=item_id,work_date=date.fromisoformat(str(form['work_date'])),manual_hours=Decimal(raw),rate_divisor=int(divisor),reason=str(form['reason']),predecessor_id=str(form['predecessor_id']) or None,**identity(request,admin))
                p=json.loads(row.proposal_json);item,run,extra,amount,source=derive(db,p)
                review=dict(id=row.id,proposal=p,extra=extra,amount=amount,expires=row.expires_at)
                return render(request,admin,item,run,current_awards(db,item_id),review)
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return failure(item_id)
    @router.post('/payroll/items/{item_id}/overtime-days/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,item_id:int,admin:AdminAccount=Depends(require_payroll_access)):
        try:
            form=await request.form();expected={'csrf_token','receipt_id','human_confirmed'}
            if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected) or form['human_confirmed']!='yes':raise ValueError('Explicit review required.')
            with SessionLocal.begin() as db:
                actor=authority(db,identity(request,admin));lock_scope(db)
                row,p=own_receipt(db,str(form['receipt_id']),actor,identity(request,admin))
                if row.item_id!=item_id:raise ValueError('Receipt item mismatch.')
                confirm_day_award(db,receipt_id=row.id,human_confirmed=True,**identity(request,admin))
            return RedirectResponse(f'/payroll/items/{item_id}/overtime-days',status_code=303,headers={'Cache-Control':'no-store'})
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return failure(item_id)
    @router.post('/payroll/items/{item_id}/overtime-days/monthly',dependencies=[Depends(csrf_protect)])
    async def monthly(request:Request,item_id:int,admin:AdminAccount=Depends(require_payroll_access)):
        try:
            form=await request.form();expected={'csrf_token','expected_digest','human_confirmed'}
            if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected) or form['human_confirmed']!='yes':raise ValueError('Explicit monthly review required.')
            with SessionLocal.begin() as db:
                actor=authority(db,identity(request,admin));lock_scope(db)
                hours,amount,digest=verified_day_totals(db,item_id)
                if form['expected_digest']!=digest:raise ValueError('Monthly days changed; review again.')
                item=stage_monthly_totals(db,item_id,rounding_contract='existing-monthly-half-up')
                from app.models import AuditLog
                run_id=item.payroll_run_id
                db.add(AuditLog(actor=actor.username,action='overtime_daily_monthly_reviewed',entity_type='payroll_item',entity_id=str(item.id),new_value=canonical(dict(digest=digest,hours=str(hours),amount=str(item.overtime_amount))),reason='Explicit monthly review of confirmed daily overtime, summed unrounded and rounded once'))
            return RedirectResponse(f'/payroll/runs/{run_id}',status_code=303,headers={'Cache-Control':'no-store'})
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return failure(item_id)
    return router
