# ruff: noqa: B008
"""Explicit HR expired-grant exclusion with fresh signed review."""
from datetime import date,datetime
from fractions import Fraction
import json
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.leave_policy_store import policy_read_guard
from app.leave_month_earning import _locks
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_exact_amount import display_days
from app.leave_carry_expiry import review_carry_expiry, apply_carry_expiry
from app.models import (CAIRO_TZ, AdminAccount, Employee, LeaveType, LeaveCarryAllocation,
    LeaveAnnualSettlement)


def carry_expiry_router(templates):
    router = APIRouter()
    signer = URLSafeTimedSerializer(settings.session_secret, salt='carry-expiry-v1')
    fields = {'grant','evaluated_on','reason'}
    def identity(request, actor):
        return dict(actor_id=actor.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def options(db, employee_id, auth):
        policy_read_guard(db,**auth)
        employee = db.get(Employee,employee_id,populate_existing=True)
        if employee is None: raise ValueError('Employee required.')
        kinds = {k.id:k for k in db.scalars(select(LeaveType).where(LeaveType.tracks_balance.is_(True))).all()}
        grants = []
        for kind,model in [('carry',LeaveCarryAllocation),('annual',LeaveAnnualSettlement)]:
            rows=db.scalars(select(model).where(model.employee_id==employee_id).order_by(model.source_year.desc(),model.id).limit(501)).all()
            if len(rows)>500: raise ValueError('Grant review scope exceeded.')
            for row in rows:
                saved=json.loads(row.result_json)
                source=saved['plan'] if kind=='annual' else saved
                amount=source['annual']['carried_days'] if kind=='annual' else source['carried_amount']
                if Fraction(amount)<=0: continue
                start=source['available_from'];end=source['available_through']
                if kind=='annual' and source['annual']['late_year_preservation']:
                    start=date(row.source_year+1,1,1).isoformat();end=date(row.source_year+1,12,31).isoformat()
                grants.append(dict(value=kind+':'+row.id,kind=kind,id=row.id,type_id=row.leave_type_id,
                    leave_type=kinds[row.leave_type_id],year=row.source_year,amount=amount,start=start,end=end))
        return employee,grants
    def selection(values,employee_id,grants):
        grant=next((v for v in grants if v['value']==values['grant']),None)
        if grant is None:raise ValueError('Choose an actual own grant.')
        return dict(grant_kind=grant['kind'],grant_id=grant['id'],employee_id=employee_id,leave_type_id=grant['type_id'],
            evaluated_on=date.fromisoformat(values['evaluated_on']),reason=values['reason'])
    def render(request,actor,employee,grants,**values):
        context=dict(admin=actor,csrf_token=get_csrf_token(request),employee=employee,grants=grants,
            grant='',evaluated_on=datetime.now(CAIRO_TZ).date().isoformat(),reason='',review=None,saved=None,proof=None,
            days=lambda v:display_days(Fraction(v)))
        context.update(values)
        response=templates.TemplateResponse(request=request,name='leave_carry_expiry.html',context=context)
        response.headers['Cache-Control']='no-store'
        return response
    def fail(exc):
        code=getattr(getattr(exc,'orig',None),'sqlstate',None)
        raise HTTPException(503 if code=='55P03' else 400,
            'تعذر استبعاد الترحيل المنتهي. راجع اكتمال الشهور وتخصيص كل الاستعمالات والمصدر والجلسة؛ لم يسجل خصم جديد.',
            headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if code=='55P03' else {})}) from exc
    root='/leave/carry-expiry/{employee_id}'
    @router.get(root)
    def page(request:Request,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                employee,grants=options(db,employee_id,identity(request,admin))
                return render(request,admin.username,employee,grants)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/review',dependencies=[Depends(csrf_protect)])
    async def review(request:Request,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope['path']!=f'/leave/carry-expiry/{employee_id}/review':raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!=fields|{'csrf_token'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact fields required.')
            values={k:str(form[k]) for k in fields}
            if any(len(v)>1100 for v in values.values()):raise ValueError('Bounded input required.')
            auth=identity(request,admin);actor=admin.username
            def transaction():
                with SessionLocal() as db:
                    bounded_workbook_operation(_locks)(db,auth)
                    employee,grants=options(db,employee_id,auth)
                    result=review_carry_expiry(db,**selection(values,employee_id,grants),**auth)
                    proof=signer.dumps(dict(employee_id=employee_id,values=values,digest=result['reviewed_digest'],
                        operation_id=secrets.token_hex(32),identity=auth))
                    return render(request,actor,employee,grants,review=result,proof=proof,**values)
            return await run_in_threadpool(transaction)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope['path']!=f'/leave/carry-expiry/{employee_id}/confirm':raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if (set(form)!={'csrf_token','proof','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form)
                    or form['human_confirmed']!='yes' or len(str(form['proof']))>12000):raise ValueError('Explicit confirmation required.')
            proof=signer.loads(str(form['proof']),max_age=1800);auth=identity(request,admin);actor=admin.username
            if (type(proof) is not dict or set(proof)!={'employee_id','values','digest','operation_id','identity'}
                    or proof['identity']!=auth or proof['employee_id']!=employee_id or set(proof['values'])!=fields):
                raise ValueError('Reviewed identity changed.')
            values=proof['values']
            def transaction():
                with SessionLocal() as db:
                    bounded_workbook_operation(_locks)(db,auth)
                    employee,grants=options(db,employee_id,auth)
                    result=apply_carry_expiry(db,operation_id=proof['operation_id'],reviewed_digest=proof['digest'],human_confirmed=True,
                        **selection(values,employee_id,grants),**auth)
                    response=render(request,actor,employee,grants,saved=result,**values)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    return router
