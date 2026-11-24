# ruff: noqa: B008
"""HR-only confirmed-source workbook and fingerprint review, no posting."""
import json
from hashlib import sha256
import re
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, or_, and_
from sqlalchemy.exc import SQLAlchemyError

from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.department_contract import export_confirmed_department, review_confirmed_department_return
from app.leave_opening_source import read_confirmed_opening_source
from app.models import AdminAccount, Department, Employee, LeaveOpeningSelection, DepartmentWorkbookPreparation, DepartmentWorkbookExport, DepartmentFingerprintPreparation


def department_workflow_router(templates):
    router=APIRouter()
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)

    @router.get('/leave/departments')
    def workspace(request: Request,department_id: int|None=None,month: str='',
                  admin: AdminAccount=Depends(require_system_admin)):
        months=[];choices=[];receipt=None;comparison=None;history=[];history_next=None;receipt_history=[];receipt_history_next=None;selected_export=''
        try:
            target=date.fromisoformat(month+'-01') if month else None
        except ValueError:
            target=None
        with SessionLocal() as db,db.no_autoflush:
            departments=db.execute(select(Department.id,Department.name).order_by(Department.name)).all()
            if target and department_id:
                months=[date(target.year,m,1) for m in range(1,target.month+1)]
                candidates=db.execute(select(LeaveOpeningSelection.id,Employee.code,Employee.full_name,Employee.id.label('employee_id'))
                    .join(Employee,Employee.id==LeaveOpeningSelection.employee_id)
                    .where(Employee.department_id==department_id,
                    LeaveOpeningSelection.restore_epoch.is_(None) if request.state.restore_epoch is None else LeaveOpeningSelection.restore_epoch==request.state.restore_epoch)
                .order_by(Employee.code,LeaveOpeningSelection.revision.desc()).limit(600)).all()
                for candidate in candidates:
                    try:
                        reviews,policy,_digest=read_confirmed_opening_source(db,selection_id=candidate.id,**identity(request,admin))
                    except ValueError:
                        continue
                    choices.append(dict(id=candidate.id,employee_id=candidate.employee_id,code=candidate.code,
                        name=candidate.full_name,basis=reviews[0]['effective_start'],insurance_enabled=policy.tier30_by_insurance_years))
            receipt_id=request.query_params.get('receipt')
            if receipt_id:
                if re.fullmatch(r'[0-9]{1,10}',receipt_id) is None or not 1<=int(receipt_id)<=2147483647:
                    raise HTTPException(status_code=404,detail='Not found')
                row=db.get(DepartmentWorkbookPreparation,int(receipt_id))
                export=db.get(DepartmentWorkbookExport,row.export_id) if row else None
                if not row or not export or export.restore_epoch!=request.state.restore_epoch:
                    raise HTTPException(status_code=404,detail='Not found')
                if row and export and export.restore_epoch==request.state.restore_epoch:
                    try:
                        if row.evidence_sha256 is None or sha256(row.evidence_json.encode()).hexdigest()!=row.evidence_sha256:
                            raise ValueError('Unverified or changed saved receipt evidence.')
                        from app.department_workbook_store import decode_receipt_evidence
                        evidence=decode_receipt_evidence(row.evidence_json)
                        raw_page=request.query_params.get('receipt_page','0')
                        if re.fullmatch(r'[0-9]{1,3}',raw_page) is None:
                            raise ValueError('Explicit bounded receipt page required.')
                        page=int(raw_page)
                        if not 0<=page<=max(0,(len(evidence)-1)//200):
                            raise ValueError('Receipt page outside saved evidence.')
                        receipt=dict(id=row.id,days=len(evidence),rows=evidence[page*200:(page+1)*200],
                            start=page*200+1 if evidence else 0,end=min((page+1)*200,len(evidence)),
                            previous=('/leave/departments?receipt='+str(row.id)+'&receipt_page='+str(page-1)) if page else None,
                            next=('/leave/departments?receipt='+str(row.id)+'&receipt_page='+str(page+1)) if (page+1)*200<len(evidence) else None)
                        selected_export=export.id
                    except (ValueError,TypeError):
                        raise HTTPException(status_code=404,detail='Not found')
            epoch=request.state.restore_epoch
            export_epoch=DepartmentWorkbookExport.restore_epoch.is_(None) if epoch is None else DepartmentWorkbookExport.restore_epoch==epoch
            receipt_query=select(DepartmentWorkbookPreparation.id,DepartmentWorkbookPreparation.created_at,
                DepartmentWorkbookPreparation.export_id).join(DepartmentWorkbookExport,
                DepartmentWorkbookExport.id==DepartmentWorkbookPreparation.export_id).where(export_epoch)
            receipt_before=request.query_params.get('receipt_before')
            if receipt_before:
                if re.fullmatch(r'[0-9]{1,10}',receipt_before) is None or not 1<=int(receipt_before)<=2147483647:
                    raise HTTPException(status_code=404,detail='Not found')
                receipt_cursor=db.execute(select(DepartmentWorkbookPreparation.id,DepartmentWorkbookPreparation.created_at)
                    .join(DepartmentWorkbookExport,DepartmentWorkbookExport.id==DepartmentWorkbookPreparation.export_id)
                    .where(export_epoch,DepartmentWorkbookPreparation.id==int(receipt_before))).first()
                if receipt_cursor is None:
                    raise HTTPException(status_code=404,detail='Not found')
                receipt_query=receipt_query.where(or_(DepartmentWorkbookPreparation.created_at<receipt_cursor.created_at,
                    and_(DepartmentWorkbookPreparation.created_at==receipt_cursor.created_at,DepartmentWorkbookPreparation.id<receipt_cursor.id)))
            saved_receipts=db.execute(receipt_query.order_by(DepartmentWorkbookPreparation.created_at.desc(),DepartmentWorkbookPreparation.id.desc()).limit(21)).all()
            receipt_history=[dict(id=h.id,created_at=h.created_at.isoformat(timespec='seconds'),export_id=h.export_id) for h in saved_receipts[:20]]
            if len(saved_receipts)>20:
                receipt_history_next='/leave/departments?receipt_before='+str(saved_receipts[19].id)
            history_query=select(DepartmentFingerprintPreparation.id,DepartmentFingerprintPreparation.created_at,
                DepartmentWorkbookPreparation.export_id).join(DepartmentWorkbookPreparation,
                DepartmentWorkbookPreparation.id==DepartmentFingerprintPreparation.return_id).join(DepartmentWorkbookExport,
                DepartmentWorkbookExport.id==DepartmentWorkbookPreparation.export_id).where(export_epoch,
                DepartmentFingerprintPreparation.restore_epoch.is_(None) if epoch is None else DepartmentFingerprintPreparation.restore_epoch==epoch)
            before=request.query_params.get('history_before')
            if before:
                cursor=db.execute(select(DepartmentFingerprintPreparation.created_at,DepartmentFingerprintPreparation.id)
                    .join(DepartmentWorkbookPreparation,DepartmentWorkbookPreparation.id==DepartmentFingerprintPreparation.return_id)
                    .join(DepartmentWorkbookExport,DepartmentWorkbookExport.id==DepartmentWorkbookPreparation.export_id).where(
                    export_epoch,DepartmentFingerprintPreparation.id==before,
                    DepartmentFingerprintPreparation.restore_epoch.is_(None) if epoch is None else DepartmentFingerprintPreparation.restore_epoch==epoch)).first()
                if cursor is None:
                    raise HTTPException(status_code=404,detail='Not found')
                history_query=history_query.where(or_(DepartmentFingerprintPreparation.created_at<cursor.created_at,
                    and_(DepartmentFingerprintPreparation.created_at==cursor.created_at,DepartmentFingerprintPreparation.id<cursor.id)))
            saved=db.execute(history_query.order_by(DepartmentFingerprintPreparation.created_at.desc(),DepartmentFingerprintPreparation.id.desc()).limit(21)).all()
            history=[dict(id=h.id,created_at=h.created_at.isoformat(timespec='seconds'),export_id=h.export_id) for h in saved[:20]]
            if len(saved)>20:
                history_next='/leave/departments?history_before='+saved[19].id
            comparison_id=request.query_params.get('comparison')
            if comparison_id:
                row=db.get(DepartmentFingerprintPreparation,comparison_id)
                if row is None or row.restore_epoch!=request.state.restore_epoch or sha256(row.review_json.encode()).hexdigest()!=row.review_sha256 or sha256(row.fingerprint_bytes).hexdigest()!=row.fingerprint_sha256:
                    raise HTTPException(status_code=404,detail='Not found')
                saved_return=db.get(DepartmentWorkbookPreparation,row.return_id)
                saved_export=db.get(DepartmentWorkbookExport,saved_return.export_id) if saved_return else None
                if saved_export is None or saved_export.restore_epoch!=epoch:
                    raise HTTPException(status_code=404,detail='Not found')
                selected_export=saved_export.id
                data=json.loads(row.review_json)
                try:
                    day_page=max(0,int(request.query_params.get('day_page','0')))
                    issue_page=max(0,int(request.query_params.get('issue_page','0')))
                    unmatched_page=max(0,int(request.query_params.get('unmatched_page','0')))
                except ValueError:
                    raise HTTPException(status_code=404,detail='Not found')
                pages={}
                from urllib.parse import urlencode
                for key,page,items,size in (('day_page',day_page,data['days'],200),('issue_page',issue_page,data['source_issues'],50),('unmatched_page',unmatched_page,data['unmatched_occurrences'],50)):
                    if page > max(0,(len(items)-1)//size):
                        raise HTTPException(status_code=404,detail='Not found')
                    base=dict(comparison=comparison_id,day_page=day_page,issue_page=issue_page,unmatched_page=unmatched_page)
                    pages[key]=dict(start=min(page*size+1,len(items)) if items else 0,end=min((page+1)*size,len(items)),total=len(items))
                    for step,label in ((-1,'previous'),(1,'next')):
                        pages[key][label]=None
                        if 0 <= page+step <= (len(items)-1)//size:
                            pages[key][label]='/leave/departments?'+urlencode({**base,key:page+step})
                comparison=dict(id=row.id,day_count=len(data['days']),days=data['days'][day_page*200:(day_page+1)*200],pages=pages,
                    issue_count=len(data['source_issues']),unmatched_count=len(data['unmatched_occurrences']),
                    issues=data['source_issues'][issue_page*50:(issue_page+1)*50],unmatched=data['unmatched_occurrences'][unmatched_page*50:(unmatched_page+1)*50])
        return templates.TemplateResponse(request=request,name='leave_departments.html',context=dict(
            admin=admin.display_name or admin.username,csrf_token=get_csrf_token(request),departments=departments,
            department_id=department_id,month=month,months=months,choices=choices,receipt=receipt,
            comparison=comparison,receipt_history=receipt_history,receipt_history_next=receipt_history_next,history=history,history_next=history_next,selected_export=selected_export,error=request.query_params.get('error')=='1'))

    @router.post('/leave/departments/export',dependencies=[Depends(csrf_protect)])
    async def export(request: Request,admin: AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            selected=tuple(form.getlist('selection_ids'))
            if not 1<=len(selected)<=300 or len(set(selected))!=len(selected) or any(re.fullmatch('[0-9a-f]{64}',v) is None for v in selected):
                raise ValueError('Exact source selection required.')
            target=date.fromisoformat(str(form['month'])+'-01')
            department_id=int(form['department_id'])
            months=[date(target.year,m,1) for m in range(1,target.month+1)]
            ages={m:date.fromisoformat(str(form['age_'+m.isoformat()])) for m in months}
            insurance={}
            with SessionLocal() as db,db.begin():
                from app.department_workbook_store import _guard
                _guard(db,**identity(request,admin))
                for selected_id in selected:
                    reviews,_policy,_digest=read_confirmed_opening_source(db,selection_id=selected_id,**identity(request,admin))
                    employee_id=reviews[0]['employee_id']
                    for m in months:
                        raw=str(form.get('insurance_'+str(employee_id)+'_'+m.isoformat(),''))
                        if raw and (len(raw)>100 or re.fullmatch(r'\d+(?:\.\d+)?',raw) is None):
                            raise ValueError('Exact insurance evidence required.')
                        insurance[employee_id,m]=Decimal(raw) if raw else None
                result=export_confirmed_department(db,selection_ids=selected,department_id=department_id,
                    month_start=target,current_insurance_years=insurance,age_evaluation_dates=ages,**identity(request,admin))
            return Response(result.payload,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                headers={'Content-Disposition':'attachment; filename="department-'+result.export_id+'.xlsx"'})
        except (ValueError,KeyError,TypeError,SQLAlchemyError):
            return RedirectResponse('/leave/departments?error=1',status_code=303)

    @router.post('/leave/departments/return',dependencies=[Depends(csrf_protect)])
    async def returned(request: Request,export_id: str=Form(...),workbook: UploadFile=File(...),
                       admin: AdminAccount=Depends(require_system_admin)):
        try:
            if re.fullmatch('[0-9a-f]{64}',export_id) is None:
                raise ValueError('Exact export reference required.')
            payload=await workbook.read(10000001)
            if not 1<=len(payload)<=10000000:
                raise ValueError('Bounded return required.')
            with SessionLocal() as db,db.begin():
                receipt=review_confirmed_department_return(db,export_id=export_id,payload=payload,**identity(request,admin))
                receipt_id=receipt.id
            return RedirectResponse('/leave/departments?receipt='+str(receipt_id),status_code=303)
        except (ValueError,SQLAlchemyError):
            return RedirectResponse('/leave/departments?error=1',status_code=303)

    @router.post('/leave/departments/compare',dependencies=[Depends(csrf_protect)])
    async def compare(request: Request,export_id: str=Form(...),department_workbook: UploadFile=File(...),
                      fingerprint_workbook: UploadFile=File(...),admin: AdminAccount=Depends(require_system_admin)):
        try:
            if re.fullmatch('[0-9a-f]{64}',export_id) is None:
                raise ValueError('Exact export reference required.')
            department_bytes=await department_workbook.read(10000001)
            fingerprint_bytes=await fingerprint_workbook.read(10000001)
            if not 1<=len(department_bytes)<=10000000 or not 1<=len(fingerprint_bytes)<=10000000:
                raise ValueError('Bounded original files required.')
            from app.department_fingerprint_workflow import compare_department_fingerprint
            with SessionLocal() as db,db.begin():
                row=compare_department_fingerprint(db,export_id=export_id,department_bytes=department_bytes,
                    fingerprint_bytes=fingerprint_bytes,fingerprint_filename=fingerprint_workbook.filename or '',**identity(request,admin))
                review_id=row.id
            return RedirectResponse('/leave/departments?comparison='+review_id,status_code=303)
        except (ValueError,SQLAlchemyError):
            return RedirectResponse('/leave/departments?error=1',status_code=303)
    return router
