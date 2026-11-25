# ruff: noqa: B008
"""Explicit carry attribution review. Never posts another balance debit."""
from datetime import date
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
from app.dependencies import require_attendance_write
from app.leave_carry_consumption import consumption_guard
from app.leave_carry_consumption import _locks
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_exact_amount import display_days
from app.leave_carry_consumption import review_carry_consumption, record_carry_consumption
from app.models import (AdminAccount, Employee, LeaveType, LeaveCarryAllocation,
    LeaveAnnualSettlement, LeaveMonthEarning, LeaveBalanceTransaction)


def carry_consumption_router(templates):
    router = APIRouter()
    signer = URLSafeTimedSerializer(settings.session_secret, salt='carry-consumption-v1')
    fields = {'grant','usage','work_date','quantity','reason'}
    def identity(request, actor):
        return dict(actor_id=actor.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def options(db, employee_id, auth):
        consumption_guard(db,writing=False,**auth)
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
        months=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee_id)
            .order_by(LeaveMonthEarning.month_start.desc()).limit(501)).all()
        if len(months)>500:raise ValueError('Monthly review scope exceeded.')
        usages=[]
        for month in months:
            for item in json.loads(month.result_json)['ledger']:
                if item['kind']!='usage':continue
                ledger=db.get(LeaveBalanceTransaction,item['id'])
                if ledger is None or ledger.leave_type_id not in kinds:continue
                usages.append(dict(value=month.id+':'+str(ledger.id),month_id=month.id,ledger_id=ledger.id,
                    type_id=ledger.leave_type_id,leave_type=kinds[ledger.leave_type_id],month=month.month_start))
        return employee,grants,usages
    def selection(values,employee_id,grants,usages):
        grant=next((v for v in grants if v['value']==values['grant']),None)
        usage=next((v for v in usages if v['value']==values['usage']),None)
        if grant is None or usage is None or grant['type_id']!=usage['type_id']:
            raise ValueError('Choose an actual own grant and same-type usage.')
        return dict(grant_kind=grant['kind'],grant_id=grant['id'],employee_id=employee_id,leave_type_id=grant['type_id'],
            monthly_receipt_id=usage['month_id'],usage_ledger_id=usage['ledger_id'],work_date=date.fromisoformat(values['work_date']),
            quantity=Fraction(values['quantity']),reason=values['reason'])
    def render(request,actor,employee,grants,usages,**values):
        context=dict(admin=actor,csrf_token=get_csrf_token(request),employee=employee,grants=grants,usages=usages,
            grant='',usage='',work_date='',quantity='',reason='',review=None,saved=None,proof=None,
            days=lambda v:display_days(Fraction(v)))
        context.update(values)
        response=templates.TemplateResponse(request=request,name='leave_carry_consumption.html',context=context)
        response.headers['Cache-Control']='no-store'
        return response
    def fail(exc):
        code=getattr(getattr(exc,'orig',None),'sqlstate',None)
        raise HTTPException(503 if code=='55P03' else 400,
            'تعذر تخصيص الاستعمال. راجع المنحة واليوم والمصدر والجلسة؛ لم يحفظ تخصيص جديد.',
            headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if code=='55P03' else {})}) from exc
    root='/leave/carry-consumption/{employee_id}'
    @router.get(root)
    def page(request:Request,employee_id:int,admin:AdminAccount=Depends(require_attendance_write)):
        try:
            with SessionLocal() as db:
                employee,grants,usages=options(db,employee_id,identity(request,admin))
                return render(request,admin.username,employee,grants,usages)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/review',dependencies=[Depends(csrf_protect)])
    async def review(request:Request,employee_id:int,admin:AdminAccount=Depends(require_attendance_write)):
        if request.scope['path']!=f'/leave/carry-consumption/{employee_id}/review':raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!=fields|{'csrf_token'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact fields required.')
            values={k:str(form[k]) for k in fields}
            if any(len(v)>1100 for v in values.values()):raise ValueError('Bounded input required.')
            auth=identity(request,admin);actor=admin.username
            def transaction():
                with SessionLocal() as db:
                    bounded_workbook_operation(_locks)(db,auth)
                    employee,grants,usages=options(db,employee_id,auth)
                    result=review_carry_consumption(db,**selection(values,employee_id,grants,usages),**auth)
                    proof=signer.dumps(dict(employee_id=employee_id,values=values,digest=result['reviewed_digest'],
                        operation_id=secrets.token_hex(32),identity=auth))
                    return render(request,actor,employee,grants,usages,review=result,proof=proof,**values)
            return await run_in_threadpool(transaction)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,employee_id:int,admin:AdminAccount=Depends(require_attendance_write)):
        if request.scope['path']!=f'/leave/carry-consumption/{employee_id}/confirm':raise HTTPException(400,'Canonical operation path required.')
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
                    employee,grants,usages=options(db,employee_id,auth)
                    result=record_carry_consumption(db,operation_id=proof['operation_id'],reviewed_digest=proof['digest'],human_confirmed=True,
                        **selection(values,employee_id,grants,usages),**auth)
                    response=render(request,actor,employee,grants,usages,saved=result,**values)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    return router
