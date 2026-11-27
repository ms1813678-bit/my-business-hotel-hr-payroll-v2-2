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


def approval_rounded_days(value):
    """Two-decimal display (ties to even) for a non-terminating rational only, else empty. Display only, never stored."""
    from fractions import Fraction
    from app.leave_exact_amount import display_days,terminating_decimal
    if isinstance(value,Fraction) and terminating_decimal(value) is None:
        return display_days(value)
    return ''


def exact_approval_days(value):
    """Lossless display, no rounding/context arithmetic or scientific notation."""
    from decimal import Decimal
    from fractions import Fraction
    if isinstance(value,Fraction):
        # Exact rational: finite decimal when one exists, otherwise numerator/denominator. Never rounded here.
        from app.leave_exact_amount import exact_text
        return exact_text(value)
    if not isinstance(value,Decimal) or not value.is_finite():
        raise ValueError('Exact finite approval amount required.')
    raw=format(value,'f')
    return raw.rstrip('0').rstrip('.') if '.' in raw else raw


def reconciliation_editor_router(templates):
    templates.env.filters['approval_exact_days']=exact_approval_days
    templates.env.filters['approval_rounded_days']=approval_rounded_days
    router=APIRouter();signer=URLSafeTimedSerializer(settings.session_secret,salt='monthly-reconciliation-draft-v1')
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def day(value):
        if not isinstance(value,str) or re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',value) is None:raise ValueError('Exact date required.')
        return date.fromisoformat(value)
    from app.leave_reconciliation_source import reconciliation_source as source
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
    def approval_render(request,actor,receipt,employee,selection,prepared=None,proof=None,approval=None,stale=False,predecessor=None):
        response=templates.TemplateResponse(request=request,name='leave_reconciliation_approval.html',context=dict(
            admin=actor.username,csrf_token=get_csrf_token(request),employee=employee,month=selection['month_start'].strftime('%Y-%m'),
            receipt=receipt,assertions=json.loads(receipt.assertions_json),prepared=prepared,approval_proof=proof,approval=approval,stale=stale,predecessor=predecessor))
        response.headers['Cache-Control']='no-store'
        return response
    @router.get('/leave/month-review/reconciliation-approval/{receipt_id}')
    def approval_page(request:Request,receipt_id:str,admin:AdminAccount=Depends(require_system_admin)):
        from app.models import LeaveReconciliationReviewReceipt,LeaveReconciliationApproval
        from app.leave_reconciliation_receipt import receipt_hash
        from app.leave_reconciliation_approval import review_approval,approval_hash
        try:
            if re.fullmatch('[0-9a-f]{64}',receipt_id) is None:raise ValueError('Exact receipt required.')
            auth=identity(request,admin)
            with SessionLocal() as db:
                db.execute(text("SET LOCAL lock_timeout='3s'"))
                from app.department_workbook_store import _guard
                _guard(db,**auth)
                row=db.get(LeaveReconciliationReviewReceipt,receipt_id,populate_existing=True)
                if row is None or receipt_hash(row)!=row.integrity_digest:raise ValueError('Intact receipt required.')
                saved=json.loads(row.assertions_json)
                employee_row=db.get(Employee,row.employee_id,populate_existing=True)
                if employee_row is None:raise ValueError('Receipt employee missing.')
                employee=dict(code=employee_row.code,name=employee_row.full_name)
                minimal=dict(month_start=day(saved['month']))
                old=db.scalar(select(LeaveReconciliationApproval).where(LeaveReconciliationApproval.receipt_id==receipt_id))
                if old and approval_hash(old)!=old.integrity_digest:raise ValueError('Approval integrity mismatch.')
                # History stays visible, but no proof is minted for stale sources.
                try:
                    selection,employee,_=source(db,row.comparison_id,row.employee_id,auth)
                    selection['release_evaluated_on']=day(saved['release_evaluated_on'])
                    _,prepared,existing,predecessor=review_approval(db,receipt_id=receipt_id,employee_id=row.employee_id,**selection,**auth)
                except (ValueError,KeyError,TypeError):
                    return approval_render(request,admin,row,employee,minimal,approval=old,stale=True)
                proof=None
                if old is None:
                    proof=signer.dumps(dict(kind='hr_approval',operation_id=secrets.token_hex(32),receipt_id=row.id,
                        receipt_digest=row.integrity_digest,comparison_digest=source(db,row.comparison_id,row.employee_id,auth)[2].source_digest,employee_id=row.employee_id,predecessor_id=predecessor,identity=auth))
                return approval_render(request,admin,row,employee,selection,prepared=prepared,proof=proof,
                    approval=old,predecessor=predecessor,stale=bool(old and existing is None))
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    @router.post('/leave/month-review/reconciliation-approve',dependencies=[Depends(csrf_protect)])
    async def approve(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        from app.models import LeaveReconciliationReviewReceipt
        from app.leave_reconciliation_approval import approve_reconciliation
        try:
            if request.scope.get('path')!='/leave/month-review/reconciliation-approve':raise ValueError('Canonical path required.')
            form=await request.form()
            if (set(form)!={'csrf_token','approval_proof','reason','human_confirmed'}
                    or any(len(form.getlist(k))!=1 for k in form) or form['human_confirmed']!='yes'
                    or len(str(form['approval_proof']))>10000):raise ValueError('Exact explicit approval required.')
            payload=signer.loads(str(form['approval_proof']),max_age=1800);auth=identity(request,admin)
            if (type(payload) is not dict or set(payload)!={'kind','operation_id','receipt_id','receipt_digest','comparison_digest','employee_id','predecessor_id','identity'}
                    or payload['kind']!='hr_approval' or payload['identity']!=auth):raise ValueError('Current reviewed HR session required.')
            reason=str(form['reason']);actor=SimpleNamespace(username=admin.username)
            def transaction():
                from app.department_workbook_store import _guard
                with SessionLocal() as db:
                    db.execute(text("SET LOCAL lock_timeout='3s'"));_guard(db,**auth)
                    receipt=db.get(LeaveReconciliationReviewReceipt,payload['receipt_id'],populate_existing=True)
                    if receipt is None:raise ValueError('Receipt missing.')
                    selection,employee,_=source(db,receipt.comparison_id,payload['employee_id'],auth)
                    selection['release_evaluated_on']=day(json.loads(receipt.assertions_json)['release_evaluated_on'])
                    row=approve_reconciliation(db,operation_id=payload['operation_id'],receipt_id=payload['receipt_id'],
                        employee_id=payload['employee_id'],reviewed_receipt_digest=payload['receipt_digest'],reviewed_comparison_digest=payload['comparison_digest'],
                        predecessor_id=payload['predecessor_id'],reason=reason,human_confirmed=True,**selection,**auth)
                    # Recheck applicability even on exact historical retry.
                    from app.leave_reconciliation_approval import review_approval
                    stale=False;prepared=None
                    try:
                        _,prepared,existing,_=review_approval(db,receipt_id=receipt.id,employee_id=receipt.employee_id,**selection,**auth)
                        stale=existing is None
                    except (ValueError,TypeError,KeyError):stale=True
                    response=approval_render(request,actor,receipt,employee,selection,prepared=prepared,approval=row,stale=stale)
                    db.commit();return response
            return await run_in_threadpool(transaction)
        except (BadSignature,ValueError,TypeError,KeyError,SQLAlchemyError) as exc:fail(exc)
    return router
