# ruff: noqa: B008
"""Fresh HR source rebind after restore. No second opening ledger credit."""
from fractions import Fraction
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
from app.leave_opening_rebind import review_opening_rebind, apply_opening_rebind
from app.models import (AdminAccount, Employee, LeaveOpeningSelection)


def opening_rebind_router(templates):
    router = APIRouter()
    signer = URLSafeTimedSerializer(settings.session_secret, salt='opening-rebind-v1')
    fields = {'old_selection','new_selection','reason'}
    def identity(request, actor):
        return dict(actor_id=actor.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def options(db, employee_id, auth):
        policy_read_guard(db,**auth)
        employee = db.get(Employee,employee_id,populate_existing=True)
        if employee is None: raise ValueError('Employee required.')
        rows=db.scalars(select(LeaveOpeningSelection).where(LeaveOpeningSelection.employee_id==employee_id)
            .order_by(LeaveOpeningSelection.created_at.desc(),LeaveOpeningSelection.id).limit(501)).all()
        if len(rows)>500:raise ValueError('Bounded selection scope required.')
        old=[row for row in rows if row.restore_epoch!=auth['expected_epoch']]
        current=[row for row in rows if row.restore_epoch==auth['expected_epoch']]
        return employee,old,current
    def selection(values,employee_id,old,current):
        previous=next((row for row in old if row.id==values['old_selection']),None)
        fresh=next((row for row in current if row.id==values['new_selection']),None)
        if previous is None or fresh is None:raise ValueError('Actual archived and fresh confirmed selections required.')
        return dict(old_selection_id=previous.id,new_selection_id=fresh.id)
    def render(request,actor,employee,old,current,**values):
        context=dict(admin=actor,csrf_token=get_csrf_token(request),employee=employee,old_sources=old,current_sources=current,
            old_selection='',new_selection='',reason='',review=None,saved=None,proof=None,
            days=lambda v:display_days(Fraction(v)))
        context.update(values)
        response=templates.TemplateResponse(request=request,name='leave_opening_rebind.html',context=context)
        response.headers['Cache-Control']='no-store'
        return response
    def fail(exc):
        code=getattr(getattr(exc,'orig',None),'sqlstate',None)
        raise HTTPException(503 if code=='55P03' else 400,
            'تعذرت إعادة ربط المصدر. راجع مطابقة الأرصدة وتاريخ البداية والسياسة والجلسة؛ لم يضف رصيد جديد.',
            headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if code=='55P03' else {})}) from exc
    root='/leave/opening-rebind/{employee_id}'
    @router.get(root)
    def page(request:Request,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                employee,old,current=options(db,employee_id,identity(request,admin))
                return render(request,admin.username,employee,old,current)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/review',dependencies=[Depends(csrf_protect)])
    async def review(request:Request,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope['path']!=f'/leave/opening-rebind/{employee_id}/review':raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!=fields|{'csrf_token'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact fields required.')
            values={k:str(form[k]) for k in fields}
            if not values['reason'].strip() or len(values['reason'])>1000:raise ValueError('Bounded reason required.')
            if any(len(v)>1100 for v in values.values()):raise ValueError('Bounded input required.')
            auth=identity(request,admin);actor=admin.username
            def transaction():
                with SessionLocal() as db:
                    bounded_workbook_operation(_locks)(db,auth)
                    employee,old,current=options(db,employee_id,auth)
                    result=review_opening_rebind(db,**selection(values,employee_id,old,current),**auth)
                    proof=signer.dumps(dict(employee_id=employee_id,values=values,digest=result['rebind_sha256'],
                        operation_id=secrets.token_hex(32),identity=auth))
                    return render(request,actor,employee,old,current,review=result,proof=proof,**values)
            return await run_in_threadpool(transaction)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope['path']!=f'/leave/opening-rebind/{employee_id}/confirm':raise HTTPException(400,'Canonical operation path required.')
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
                    employee,old,current=options(db,employee_id,auth)
                    result=apply_opening_rebind(db,reviewed_rebind_sha256=proof['digest'],human_confirmed=True,reason=values['reason'],
                        **selection(values,employee_id,old,current),**auth)
                    response=render(request,actor,employee,old,current,saved=result,**values)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    return router
