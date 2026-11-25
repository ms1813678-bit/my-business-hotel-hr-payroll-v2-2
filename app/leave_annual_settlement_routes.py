# ruff: noqa: B008
"""Normal HR annual settlement review and explicit atomic confirmation."""
from datetime import date
from decimal import Decimal
from fractions import Fraction
from app.leave_exact_amount import display_days
import json
import re
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
from app.department_workbook_store import _guard
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_policy_store import policy_read_guard
from app.leave_annual_settlement_store import review_annual_settlement_apply, apply_annual_settlement
from app.models import AdminAccount, Employee, LeaveType, LeaveOpeningSelection, LeaveMonthEarning, PayrollItem, PayrollRun


def annual_settlement_router(templates):
    router = APIRouter()
    signer = URLSafeTimedSerializer(settings.session_secret, salt='annual-settlement-v1')
    def identity(request, admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def source(db, selection_id, reviewed_on, auth, *, writing):
        (bounded_workbook_operation(_guard) if writing else policy_read_guard)(db,**auth)
        if re.fullmatch('[0-9a-f]{64}',selection_id) is None:
            raise ValueError('Canonical source selection required.')
        opening=db.get(LeaveOpeningSelection,selection_id,populate_existing=True)
        if opening is None:
            raise ValueError('Confirmed source selection required.')
        employee=db.get(Employee,opening.employee_id,populate_existing=True)
        monthly=db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id==employee.id,
            LeaveMonthEarning.selection_id==selection_id).order_by(LeaveMonthEarning.month_start.desc()).limit(1)).first()
        if monthly is None or monthly.month_start.month!=12:
            raise ValueError('Completed December source required before annual settlement.')
        saved=json.loads(monthly.source_json)
        kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
        if kind is None:
            raise ValueError('Actual annual leave type required.')
        selected=dict(employee_id=employee.id,leave_type_id=kind.id,source_year=monthly.month_start.year,
            selection_id=selection_id,reviewed_on=reviewed_on,
            current_insurance_years={(i,date.fromisoformat(m)):Decimal(v) if v is not None else None
                for i,m,v in saved['current_insurance_years']},
            age_evaluation_dates={date.fromisoformat(m):date.fromisoformat(d) for m,d in saved['age_evaluation_dates']},**auth)
        items=db.execute(select(PayrollItem,PayrollRun).join(PayrollRun,PayrollRun.id==PayrollItem.payroll_run_id)
            .where(PayrollItem.employee_id==employee.id).order_by(PayrollRun.period_start.desc())).all()
        return selected,employee,saved['tier'],items
    def render(request,actor,**values):
        context=dict(admin=actor,csrf_token=get_csrf_token(request),review=None,saved=None,proof=None,
            reviewed_on='',payroll_item_id='',available_from='',available_through='',reason='', days=lambda value:display_days(Fraction(value)))
        context.update(values)
        response=templates.TemplateResponse(request=request,name='leave_annual_settlement.html',context=context)
        response.headers['Cache-Control']='no-store'
        return response
    def fail(exc):
        code=getattr(getattr(exc,'orig',None),'sqlstate',None)
        raise HTTPException(503 if code=='55P03' else 400,
            'تعذرت التسوية السنوية. راجع مصدر السنة والرصيد والجلسة؛ لا توجد تسوية جديدة محفوظة.',
            headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if code=='55P03' else {})}) from exc
    root='/leave/annual-settlement/{selection_id}'
    @router.get(root)
    def page(request:Request,selection_id:str,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                _,employee,tier,items=source(db,selection_id,None,identity(request,admin),writing=False)
                return render(request,admin.username,selection_id=selection_id,employee=employee,tier=tier,items=items)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/review',dependencies=[Depends(csrf_protect)])
    async def review(request:Request,selection_id:str,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope['path']!=f'/leave/annual-settlement/{selection_id}/review':
            raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','reviewed_on','payroll_item_id','available_from','available_through','reason'} or any(len(form.getlist(k))!=1 for k in form):
                raise ValueError('Exact annual review fields required.')
            values={k:str(form[k]) for k in form if k!='csrf_token'}
            reviewed_on=date.fromisoformat(values['reviewed_on'])
            item=int(values['payroll_item_id']) if values['payroll_item_id'] else None
            start=date.fromisoformat(values['available_from']) if values['available_from'] else None
            end=date.fromisoformat(values['available_through']) if values['available_through'] else None
            auth=identity(request,admin);actor=admin.username
            def transaction():
                with SessionLocal() as db:
                    selected,employee,tier,items=source(db,selection_id,reviewed_on,auth,writing=True)
                    result=review_annual_settlement_apply(db,source_selection=selected,full_annual_entitlement=tier,
                        payroll_item_id=item,available_from=start,available_through=end,reason=values['reason'])
                    proof=signer.dumps(dict(selection_id=selection_id,values=values,digest=result['reviewed_digest'],
                        operation_id=secrets.token_hex(32),identity=auth))
                    return render(request,actor,selection_id=selection_id,employee=employee,tier=tier,items=items,
                        review=result,proof=proof,**values)
            return await run_in_threadpool(transaction)
        except (ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,selection_id:str,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope['path']!=f'/leave/annual-settlement/{selection_id}/confirm':
            raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','proof','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form) or form['human_confirmed']!='yes' or len(str(form['proof']))>12000:
                raise ValueError('Explicit annual confirmation required.')
            proof=signer.loads(str(form['proof']),max_age=1800);auth=identity(request,admin);actor=admin.username
            if (type(proof) is not dict or set(proof)!={'selection_id','values','digest','operation_id','identity'}
                    or proof['identity']!=auth or proof['selection_id']!=selection_id):
                raise ValueError('Annual review identity changed.')
            values=proof['values']
            def transaction():
                with SessionLocal() as db:
                    selected,employee,tier,items=source(db,selection_id,date.fromisoformat(values['reviewed_on']),auth,writing=True)
                    result=apply_annual_settlement(db,operation_id=proof['operation_id'],reviewed_digest=proof['digest'],human_confirmed=True,
                        source_selection=selected,full_annual_entitlement=tier,payroll_item_id=int(values['payroll_item_id']) if values['payroll_item_id'] else None,
                        available_from=date.fromisoformat(values['available_from']) if values['available_from'] else None,
                        available_through=date.fromisoformat(values['available_through']) if values['available_through'] else None,reason=values['reason'])
                    response=render(request,actor,selection_id=selection_id,employee=employee,tier=tier,items=items,saved=result)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ArithmeticError,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    return router
