"""Owner-selected dedicated monthly review, no balance release/month-close UI.

Read overview is HR-only. Register review/confirm remains default-off synthetic
cycle only. Whole DB/parser/render transactions run outside the async loop.
"""
from datetime import date
from types import SimpleNamespace
import re,secrets
from fastapi import APIRouter,Depends,Request,HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from itsdangerous import URLSafeTimedSerializer,BadSignature
from app.config import settings
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.models import (AdminAccount,DepartmentFingerprintPreparation,DepartmentWorkbookPreparation,
    DepartmentWorkbookExport,DepartmentPreparationContract,LeaveSyntheticRegisterConsumption,
    EmployeeServicePeriod,Employee,LeaveMonthClose,LeaveReconciliationReviewReceipt)
from app.department_manifest import decode_manifest
from app.leave_policy_store import policy_read_guard
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_synthetic_register import preview_synthetic_register,consume_synthetic_register


def month_value(value):
    if not isinstance(value,str) or re.fullmatch('[0-9]{4}-[0-9]{2}',value) is None:
        raise ValueError('Exact calendar month required.')
    return date.fromisoformat(value+'-01')


def enabled():
    from sqlalchemy.engine import make_url
    return (settings.leave_synthetic_register_enabled and settings.app_env.strip().lower() in {'test','synthetic'}
        and (make_url(settings.database_url).database or '').startswith('synthetic_cycle_'))


def monthly_review_router(templates):
    router=APIRouter()
    signer=URLSafeTimedSerializer(settings.session_secret,salt='monthly-register-review-v1')
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def render(request,actor,**values):
        values['close_preview']=values.pop('_close_preview',None)
        values['close_stage']=values.pop('_close_stage','readiness')
        response=templates.TemplateResponse(request=request,name='leave_month_review.html',context=dict(
            admin=actor.username,csrf_token=get_csrf_token(request),operational=enabled(),
            month_names=('يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'),**values))
        response.headers['Cache-Control']='no-store';return response
    @bounded_workbook_operation
    def overview(db,month,auth):
        policy_read_guard(db,**auth)
        rows=[];truncated=False;global_count=None;closed=False
        if month:
            target=month_value(month)
            from calendar import monthrange
            last=date(target.year,target.month,monthrange(target.year,target.month)[1])
            ids=set(db.scalars(select(EmployeeServicePeriod.employee_id).where(EmployeeServicePeriod.start_date<=last,
                (EmployeeServicePeriod.end_date.is_(None)|(EmployeeServicePeriod.end_date>=target)))))
            global_count=len(ids)
            closed=db.scalar(select(LeaveMonthClose.id).where(LeaveMonthClose.year==target.year,LeaveMonthClose.month==target.month)) is not None
            candidates=db.execute(select(DepartmentFingerprintPreparation,DepartmentWorkbookPreparation,DepartmentWorkbookExport)
                .join(DepartmentWorkbookPreparation,DepartmentWorkbookPreparation.id==DepartmentFingerprintPreparation.return_id)
                .join(DepartmentWorkbookExport,DepartmentWorkbookExport.id==DepartmentWorkbookPreparation.export_id)
                .where(DepartmentFingerprintPreparation.restore_epoch.is_(None) if auth['expected_epoch'] is None else DepartmentFingerprintPreparation.restore_epoch==auth['expected_epoch'])
                .order_by(DepartmentFingerprintPreparation.created_at.desc(),DepartmentFingerprintPreparation.id).limit(301)).all()
            truncated=len(candidates)>300
            scanned=0
            for compared,returned,exported in candidates[:300]:
                if exported.invalidated_at is not None or exported.restore_epoch!=auth['expected_epoch']:continue
                from hashlib import sha256
                if sha256(exported.manifest_json.encode()).hexdigest()!=exported.manifest_sha256:continue
                manifest=decode_manifest(exported.manifest_json)
                scanned+=len(manifest.employees)
                if scanned>10000:raise ValueError('Narrow comparison source scope required.')
                if manifest.month_start!=target:continue
                contract=db.get(DepartmentPreparationContract,exported.id)
                selected={}
                if contract and sha256(contract.selection_json.encode()).hexdigest()==contract.selection_sha256:
                    from app.department_contract import decode_selection
                    from app.models import LeaveOpeningSelection
                    for sid in decode_selection(contract.selection_json)['selection_ids']:
                        source=db.get(LeaveOpeningSelection,sid)
                        if source:selected[source.employee_id]=sid
                for e in manifest.employees:
                    previous=db.scalar(select(LeaveSyntheticRegisterConsumption.id).where(
                        LeaveSyntheticRegisterConsumption.comparison_id==compared.id,LeaveSyntheticRegisterConsumption.employee_id==e.employee_id))
                    assertion_receipts=db.scalars(select(LeaveReconciliationReviewReceipt).where(LeaveReconciliationReviewReceipt.comparison_id==compared.id,LeaveReconciliationReviewReceipt.employee_id==e.employee_id,LeaveReconciliationReviewReceipt.restore_epoch.is_(None) if auth['expected_epoch'] is None else LeaveReconciliationReviewReceipt.restore_epoch==auth['expected_epoch']).order_by(LeaveReconciliationReviewReceipt.created_at.desc()).limit(4)).all()
                    rows.append(dict(assertion_receipts=[dict(id=r.id,created_at=r.created_at) for r in assertion_receipts],comparison_id=compared.id,employee_id=e.employee_id,code=e.code,name=e.name,
                        department=manifest.department,previous=bool(previous),selection_id=selected.get(e.employee_id)))
                    if len(rows)>500:raise ValueError('Narrow monthly review scope required.')
        policy_read_guard(db,**auth)
        return dict(month=month,rows=rows,truncated=truncated,global_count=global_count,closed=closed,review=None,saved=False)
    def failure(exc):
        code=getattr(getattr(exc,'orig',None),'sqlstate',None)
        raise HTTPException(status_code=503 if code=='55P03' else 400,detail='تعذرت مراجعة الشهر. راجع المصدر والفترة والجلسة.',
            headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if code=='55P03' else {})}) from exc
    @router.get('/leave/month-review')
    def page(request:Request,month:str='',year:str='',month_number:str='',close_stage:str='readiness',admin:AdminAccount=Depends(require_system_admin)):
        try:
            if year or month_number:
                if month or re.fullmatch('[0-9]{4}',year) is None or re.fullmatch('0[1-9]|1[0-2]',month_number) is None:raise ValueError('Exact month/year required.')
                month=year+'-'+month_number
            if month:month_value(month)
            if close_stage not in {'readiness','summary'}:raise ValueError('Exact preview stage required.')
            with SessionLocal() as db:
                auth=identity(request,admin)
                values=overview(db,month,auth)
                if month:
                    from app.leave_close_day_preview import read_close_day_preview
                    values['close_preview']=read_close_day_preview(db,month=month_value(month),**auth)
                # Render context defaults are supplied once below.
                preview=values.pop('close_preview',None)
                return render(request,admin,**values,_close_preview=preview,_close_stage=close_stage)
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:failure(exc)
    @router.post('/leave/month-review/register-review',dependencies=[Depends(csrf_protect)])
    async def prepare(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        if not enabled():raise HTTPException(status_code=404,detail='Not enabled.')
        if request.scope.get('path')!='/leave/month-review/register-review':
            raise HTTPException(status_code=400,detail='Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','month','comparison_id','employee_id','reason'} or any(len(form.getlist(k))!=1 for k in form):
                raise ValueError('Exact review fields required.')
            month=str(form['month']);target=month_value(month);comparison_id=str(form['comparison_id']);raw=str(form['employee_id']);reason=str(form['reason']).strip()
            if re.fullmatch('[0-9a-f]{64}',comparison_id) is None or re.fullmatch('[1-9][0-9]{0,9}',raw) is None or int(raw)>2147483647 or not reason or len(reason)>1000:
                raise ValueError('Exact bounded review scope required.')
            employee_id=int(raw);auth=identity(request,admin);actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    proposals,digest=preview_synthetic_register(db,comparison_id=comparison_id,employee_id=employee_id,**auth)
                    if not proposals or any(date.fromisoformat(p['date']).replace(day=1)!=target for p in proposals):
                        raise ValueError('Month/source mismatch.')
                    employee=db.get(Employee,employee_id,populate_existing=True)
                    proof=signer.dumps(dict(operation_id=secrets.token_hex(32),comparison_id=comparison_id,employee_id=employee_id,
                        month=month,digest=digest,reason=reason,identity=auth))
                    values=overview(db,month,auth)
                    values['review']=dict(proposals=proposals,proof=proof,reason=reason,name=employee.full_name,code=employee.code,comparison_id=comparison_id,employee_id=employee_id)
                    return render(request,actor,**values)
            return await run_in_threadpool(transaction)
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:failure(exc)
    @router.post('/leave/month-review/register-confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        if not enabled():raise HTTPException(status_code=404,detail='Not enabled.')
        if request.scope.get('path')!='/leave/month-review/register-confirm':
            raise HTTPException(status_code=400,detail='Canonical operation path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','proof','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form) or form['human_confirmed']!='yes' or len(str(form['proof']))>10000:
                raise ValueError('Exact explicit confirmation required.')
            proof=signer.loads(str(form['proof']),max_age=1800);auth=identity(request,admin)
            if type(proof) is not dict or set(proof)!={'operation_id','comparison_id','employee_id','month','digest','reason','identity'} or proof['identity']!=auth:
                raise ValueError('Review identity changed.')
            target=month_value(proof['month']);actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    # The signed month was checked against every reviewed day;
                    # consume rechecks that exact current source under bounded locks.
                    consume_synthetic_register(db,operation_id=proof['operation_id'],comparison_id=proof['comparison_id'],employee_id=proof['employee_id'],
                        reviewed_digest=proof['digest'],human_confirmed=True,reason=proof['reason'],**auth)
                    values=overview(db,proof['month'],auth);values['saved']=True
                    response=render(request,actor,**values)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ValueError,TypeError,KeyError,SQLAlchemyError) as exc:failure(exc)
    return router
