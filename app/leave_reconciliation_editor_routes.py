"""Monthly evidence editor: drafts and assertion-only receipts, no ledger effects."""
from datetime import date
from hashlib import sha256
from types import SimpleNamespace
import re,secrets,json
from dataclasses import asdict
from fastapi import APIRouter,Request,Depends,HTTPException
from itsdangerous import URLSafeTimedSerializer,BadSignature
from sqlalchemy import select,text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.models import (AdminAccount,Employee,LeaveOpeningSelection,DepartmentFingerprintPreparation,
    DepartmentWorkbookPreparation,DepartmentPreparationContract,LeaveBalanceTransaction)
from app.department_contract import decode_selection
from app.department_workbook_guard import bounded_workbook_operation
from app.shift_day_store import read_saved_shift_days
from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_transition_draft import draft_transition,LedgerDecision,OpeningReleaseDecision


def reconciliation_editor_router(templates):
    router=APIRouter();signer=URLSafeTimedSerializer(settings.session_secret,salt='monthly-reconciliation-draft-v1')
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def day(value):
        if not isinstance(value,str) or re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',value) is None:raise ValueError('Exact date required.')
        return date.fromisoformat(value)
    @bounded_workbook_operation
    def source(db,comparison_id,employee_id,auth):
        if re.fullmatch('[0-9a-f]{64}',comparison_id) is None or not 0<employee_id<=2147483647:raise ValueError('Exact source required.')
        raw=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**auth)
        comparison=db.get(DepartmentFingerprintPreparation,comparison_id,populate_existing=True)
        returned=db.get(DepartmentWorkbookPreparation,comparison.return_id,populate_existing=True)
        contract=db.get(DepartmentPreparationContract,returned.export_id,populate_existing=True)
        if contract is None or sha256(contract.selection_json.encode()).hexdigest()!=contract.selection_sha256:raise ValueError('Confirmed source contract required.')
        selection=decode_selection(contract.selection_json)
        selected=db.scalars(select(LeaveOpeningSelection).where(LeaveOpeningSelection.id.in_(selection['selection_ids']),LeaveOpeningSelection.employee_id==employee_id)).all()
        if len(selected)!=1:raise ValueError('One exact selected employee source required.')
        selection['selection_ids']=(selected[0].id,)
        selection['current_insurance_years']={k:v for k,v in selection['current_insurance_years'].items() if k[0]==employee_id}
        employee=db.get(Employee,employee_id,populate_existing=True)
        return selection,dict(code=employee.code,name=employee.full_name),raw
    def render(request,actor,comparison_id,employee_id,selection,employee,result=None,proof=None,ledger=(),draft=None,decisions=(),record_proof=None,receipt=None,stale=False):
        response=templates.TemplateResponse(request=request,name='leave_reconciliation_editor.html',context=dict(
            admin=actor.username,csrf_token=get_csrf_token(request),comparison_id=comparison_id,employee_id=employee_id,
            month=selection['month_start'].strftime('%Y-%m'),selection=selection,employee=employee,result=result,proof=proof,ledger=ledger,draft=draft,decisions=decisions,record_proof=record_proof,receipt=receipt,stale=stale))
        response.headers['Cache-Control']='no-store';return response
    def fail(exc):
        busy=getattr(getattr(exc,'orig',None),'sqlstate',None)=='55P03'
        raise HTTPException(status_code=503 if busy else 400,detail='تعذرت مسودة التسوية. راجع المصدر والأدلة والجلسة.',headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if busy else {})}) from exc
    @router.get('/leave/month-review/reconciliation/{comparison_id}/{employee_id}')
    def page(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                selection,employee,_=source(db,comparison_id,employee_id,identity(request,admin))
                return render(request,admin,comparison_id,employee_id,selection,employee)
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    @router.post('/leave/month-review/reconciliation-snapshot',dependencies=[Depends(csrf_protect)])
    async def snapshot(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope.get('path')!='/leave/month-review/reconciliation-snapshot':raise HTTPException(400,'Canonical path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','comparison_id','employee_id','release_evaluated_on'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact fields required.')
            comparison_id=str(form['comparison_id']);employee_id=int(str(form['employee_id']));release=day(str(form['release_evaluated_on']))
            auth=identity(request,admin);actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    selection,employee,_=source(db,comparison_id,employee_id,auth)
                    result=reconcile_selected_opening(db,**selection,release_evaluated_on=release,**auth)
                    ids={i for r in result.rows for i in r.ledger_row_ids}
                    if len(ids)>200:raise ValueError('Narrow evidence scope needed before editor.')
                    ledger=db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.id.in_(ids)).order_by(LeaveBalanceTransaction.id)).all()
                    proof=signer.dumps(dict(comparison_id=comparison_id,employee_id=employee_id,release=release.isoformat(),digest=result.reconciliation_digest,identity=auth))
                    return render(request,actor,comparison_id,employee_id,selection,employee,result,proof,ledger)
            return await run_in_threadpool(transaction)
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    @router.post('/leave/month-review/reconciliation-draft',dependencies=[Depends(csrf_protect)])
    async def draft(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope.get('path')!='/leave/month-review/reconciliation-draft':raise HTTPException(400,'Canonical path required.')
        try:
            form=await request.form()
            if 'proof' not in form or len(str(form['proof']))>10000:raise ValueError('Bounded snapshot proof required.')
            proof=signer.loads(str(form['proof']),max_age=1800);auth=identity(request,admin)
            if type(proof) is not dict or set(proof)!={'comparison_id','employee_id','release','digest','identity'} or proof['identity']!=auth:raise ValueError('Current snapshot session required.')
            actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    selection,employee,_=source(db,proof['comparison_id'],proof['employee_id'],auth)
                    selection['release_evaluated_on']=day(proof['release'])
                    result=reconcile_selected_opening(db,**selection,**auth)
                    if result.reconciliation_digest!=proof['digest']:raise ValueError('Reviewed source changed.')
                    ids={i for r in result.rows for i in r.ledger_row_ids}
                    if len(ids)>200:raise ValueError('Narrow evidence scope required.')
                    expected={'csrf_token','proof'}|{f'opening_{r.code}_{suffix}' for r in result.rows for suffix in ('value','reference')}|{f'ledger_{i}_{suffix}' for i in ids for suffix in ('value','reference')}
                    if set(form)!=expected or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact decision coverage required.')
                    openings=[];decisions=[]
                    for r in result.rows:
                        value=str(form[f'opening_{r.code}_value'])
                        if value not in {'unknown','released','not_released'}:raise ValueError('Exact opening decision required.')
                        openings.append(OpeningReleaseDecision(r.employee_id,r.code,{'unknown':None,'released':True,'not_released':False}[value],str(form[f'opening_{r.code}_reference'])))
                    for i in sorted(ids):decisions.append(LedgerDecision(i,str(form[f'ledger_{i}_value']),str(form[f'ledger_{i}_reference'])))
                    prepared=draft_transition(db,reviewed_reconciliation_digest=result.reconciliation_digest,ledger_decisions=tuple(decisions),opening_decisions=tuple(openings),**selection,**auth)
                    record_proof=signer.dumps(dict(kind='assertions',operation_id=secrets.token_hex(32),snapshot=proof,decision_digest=prepared.decision_digest,openings=[asdict(d) for d in openings],ledger=[asdict(d) for d in decisions]))
                    return render(request,actor,proof['comparison_id'],proof['employee_id'],selection,employee,result,draft=prepared,record_proof=record_proof,decisions=tuple(
                        [('opening',d.code,{'unknown':'غير محسوم','released':'يوجد دليل إفراج','not_released':'غير مُفرج عنه'}['unknown' if d.released is None else 'released' if d.released else 'not_released'],d.evidence_reference) for d in openings]+
                        [('ledger',d.ledger_id,{'unresolved':'غير محسوم','included_in_scenario':'محسوبة داخل السيناريو','external_to_scenario':'مستقلة عن السيناريو'}[d.treatment],d.evidence_reference) for d in decisions]))
            return await run_in_threadpool(transaction)
        except (BadSignature,ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    @router.post('/leave/month-review/reconciliation-record',dependencies=[Depends(csrf_protect)])
    async def record(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        if request.scope.get('path')!='/leave/month-review/reconciliation-record':raise HTTPException(400,'Canonical path required.')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','record_proof','reason','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form) or form['human_confirmed']!='yes' or len(str(form['record_proof']))>600000:raise ValueError('Exact explicit assertion recording required.')
            payload=signer.loads(str(form['record_proof']),max_age=1800);auth=identity(request,admin)
            if type(payload) is not dict or set(payload)!={'kind','operation_id','snapshot','decision_digest','openings','ledger'} or payload['kind']!='assertions':raise ValueError('Exact reviewed assertion payload required.')
            proof=payload['snapshot']
            if proof['identity']!=auth:raise ValueError('Current reviewed session required.')
            actor=SimpleNamespace(username=admin.username);reason=str(form['reason'])
            def transaction():
                from app.department_workbook_store import _guard
                from app.leave_reconciliation_receipt import prepare_assertion_receipt
                with SessionLocal() as db:
                    db.execute(text("SET LOCAL lock_timeout='3s'"))
                    _guard(db,**auth)
                    selection,employee,_=source(db,proof['comparison_id'],proof['employee_id'],auth)
                    selection['release_evaluated_on']=day(proof['release'])
                    row=prepare_assertion_receipt(db,operation_id=payload['operation_id'],comparison_id=proof['comparison_id'],employee_id=proof['employee_id'],
                        reviewed_source_digest=proof['digest'],reviewed_decision_digest=payload['decision_digest'],
                        ledger_decisions=tuple(LedgerDecision(**d) for d in payload['ledger']),opening_decisions=tuple(OpeningReleaseDecision(**d) for d in payload['openings']),
                        reason=reason,human_confirmed=True,**selection,**auth)
                    response=render(request,actor,proof['comparison_id'],proof['employee_id'],selection,employee,receipt=dict(id=row.id,reason=row.reason,created_at=row.created_at,assertions=json.loads(row.assertions_json)))
                    db.commit();return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    @router.get('/leave/month-review/reconciliation-receipt/{receipt_id}')
    def receipt_page(request:Request,receipt_id:str,admin:AdminAccount=Depends(require_system_admin)):
        from app.models import LeaveReconciliationReviewReceipt
        from app.leave_reconciliation_receipt import receipt_hash
        from app.leave_policy_store import policy_read_guard
        try:
            if re.fullmatch('[0-9a-f]{64}',receipt_id) is None:raise ValueError('Exact receipt required.')
            auth=identity(request,admin)
            with SessionLocal() as db:
                policy_read_guard(db,**auth)
                row=db.get(LeaveReconciliationReviewReceipt,receipt_id,populate_existing=True)
                if row is None or row.restore_epoch!=auth['expected_epoch'] or receipt_hash(row)!=row.integrity_digest:raise ValueError('Receipt source unavailable.')
                employee_row=db.get(Employee,row.employee_id,populate_existing=True)
                if employee_row is None:raise ValueError('Receipt employee missing.')
                employee=dict(code=employee_row.code,name=employee_row.full_name)
                assertions=json.loads(row.assertions_json);selection=dict(month_start=day(assertions['month']))
                return render(request,admin,row.comparison_id,row.employee_id,selection,employee,receipt=dict(id=row.id,reason=row.reason,created_at=row.created_at,assertions=assertions),stale=True)
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    return router
