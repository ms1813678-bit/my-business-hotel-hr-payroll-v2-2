# ruff: noqa: B008
"""HR exact monthly review/commit on saved confirmed export contracts."""
import re
import secrets
from datetime import date
from fractions import Fraction
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.department_contract import decode_selection
from app.department_workbook_store import _guard
from app.dependencies import require_system_admin
from app.leave_exact_amount import display_days
from app.leave_month_earning import (
    apply_annual_release,
    apply_month_earning,
    review_annual_release,
    review_month_earning,
)
from app.models import (
    AdminAccount,
    DepartmentFingerprintPreparation,
    DepartmentPreparationContract,
    DepartmentWorkbookPreparation,
    Employee,
    LeaveOpeningSelection,
)


def monthly_earning_router(templates):
    router=APIRouter()
    signer=URLSafeTimedSerializer(settings.session_secret,salt='monthly-exact-earning-v1')
    def identity(request,admin):
        return {'actor_id': admin.id,'session_id': request.session.get('admin_session_id'),
            'session_version': request.session.get('admin_session_version'),'expected_epoch': request.state.restore_epoch}
    def source(db,comparison_id,employee_id,evaluated_on,auth,*,writing=False):
        if writing:
            from app.department_workbook_guard import bounded_workbook_operation
            bounded_workbook_operation(_guard)(db,**auth)
        else:
            from app.leave_policy_store import policy_read_guard
            policy_read_guard(db,**auth)
        if (not isinstance(comparison_id,str) or re.fullmatch('[0-9a-f]{64}',comparison_id) is None
                or type(employee_id) is not int or employee_id<1 or employee_id>2147483647):raise ValueError('Resolved source required.')
        comparison=db.get(DepartmentFingerprintPreparation,comparison_id,populate_existing=True)
        returned=db.get(DepartmentWorkbookPreparation,comparison.return_id,populate_existing=True) if comparison else None
        contract=db.get(DepartmentPreparationContract,returned.export_id,populate_existing=True) if returned else None
        if (comparison is None or comparison.restore_epoch!=auth['expected_epoch'] or contract is None
                or sha256(contract.selection_json.encode()).hexdigest()!=contract.selection_sha256):raise ValueError('Saved current source contract required.')
        selection=decode_selection(contract.selection_json)
        selected=[s for s in selection['selection_ids'] if db.get(LeaveOpeningSelection,s).employee_id==employee_id]
        employee=db.get(Employee,employee_id,populate_existing=True)
        if len(selected)!=1 or employee is None:raise ValueError('Exact selected employee source required.')
        return dict(selection_id=selected[0],month_start=selection['month_start'],evaluated_on=evaluated_on,
            current_insurance_years={k:v for k,v in selection['current_insurance_years'].items() if k[0]==employee_id},
            age_evaluation_dates=selection['age_evaluation_dates'],**auth),employee
    def render(request,actor,**values):
        context={'admin': actor,'csrf_token': get_csrf_token(request),'display_days': display_days,'Fraction': Fraction,
            'review': None,'proof': None,'saved': None,'reason': '','mode': 'earning','evaluated_on': ''}
        context.update(values)
        response=templates.TemplateResponse(request=request,name='leave_month_earning.html',context=context)
        response.headers['Cache-Control']='no-store';return response
    def fail(exc):
        code=getattr(getattr(exc,'orig',None),'sqlstate',None)
        raise HTTPException(status_code=503 if code=='55P03' else 400,detail='تعذرت مراجعة الاستحقاق. راجع المصدر والفترة والجلسة.',
            headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if code=='55P03' else {})}) from exc
    root='/leave/month-review/earning/{comparison_id}/{employee_id}'
    @router.get(root)
    def page(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                auth=identity(request,admin)
                _selection,employee=source(db,comparison_id,employee_id,None,auth)
                return render(request,admin.username,comparison_id=comparison_id,employee=employee)
        except (ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/review',dependencies=[Depends(csrf_protect)])
    async def review(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope.get('path')!=f'/leave/month-review/earning/{comparison_id}/{employee_id}/review':raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','evaluated_on','reason','mode'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact fields required.')
            evaluated=date.fromisoformat(str(form['evaluated_on']));reason=str(form['reason']).strip();mode=str(form['mode'])
            if not reason or len(reason)>1000 or mode not in {'earning','release'}:raise ValueError('Explicit bounded review required.')
            auth=identity(request,admin);actor=admin.username
            def transaction():
                with SessionLocal() as db:
                    selection,employee=source(db,comparison_id,employee_id,evaluated,auth,writing=True)
                    result=(review_month_earning if mode=='earning' else review_annual_release)(db,**selection)
                    proof=signer.dumps({'comparison_id': comparison_id,'employee_id': employee_id,'evaluated_on': evaluated.isoformat(),'reason': reason,
                        'mode': mode,'digest': result['reviewed_digest'],'operation_id': secrets.token_hex(32),'identity': auth})
                    return render(request,actor,comparison_id=comparison_id,employee=employee,**{'review': result,'proof': proof,'reason': reason,'mode': mode,'evaluated_on': evaluated.isoformat()})
            return await run_in_threadpool(transaction)
        except (ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    @router.post(root+'/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope.get('path')!=f'/leave/month-review/earning/{comparison_id}/{employee_id}/confirm':raise HTTPException(400,'Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','proof','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form) or form['human_confirmed']!='yes' or len(str(form['proof']))>12000:raise ValueError('Explicit confirmation required.')
            proof=signer.loads(str(form['proof']),max_age=1800);auth=identity(request,admin);actor=admin.username
            if (type(proof) is not dict or set(proof)!={'comparison_id','employee_id','evaluated_on','reason','mode','digest','operation_id','identity'}
                    or proof['identity']!=auth or proof['comparison_id']!=comparison_id or proof['employee_id']!=employee_id
                    or proof['mode'] not in {'earning','release'}):raise ValueError('Review identity changed.')
            def transaction():
                with SessionLocal() as db:
                    selection,employee=source(db,comparison_id,employee_id,date.fromisoformat(proof['evaluated_on']),auth,writing=True)
                    result=(apply_month_earning if proof['mode']=='earning' else apply_annual_release)(db,operation_id=proof['operation_id'],
                        reviewed_digest=proof['digest'],human_confirmed=True,reason=proof['reason'],**selection)
                    response=render(request,actor,comparison_id=comparison_id,employee=employee,saved=result)
                    db.commit();return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ValueError,TypeError,KeyError,AttributeError,SQLAlchemyError) as exc:fail(exc)
    return router
