# ruff: noqa: B008
"""Owner-selected saved-day-list/detail, HR review only, no writes."""
from datetime import date,timedelta
from types import SimpleNamespace
from sqlalchemy.exc import OperationalError
import re,secrets,json
from sqlalchemy.engine import make_url
from app.config import settings
from app.attendance_evidence_review import prepare_evidence_review,consume_evidence_review,_identity
from app.models import AttendanceEvidenceReviewReceipt
from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter,Depends,Request,HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from app.db import SessionLocal
from app.models import AdminAccount,Employee,ShiftPattern
from app.dependencies import require_system_admin
from app.csrf import csrf_protect,get_csrf_token
from app.shift_day_store import read_saved_shift_days
from app.shift_evidence_source import preview_configured_shift_evidence
from app.fingerprint_bounded_parse import FingerprintPlatformUnavailable,PLATFORM_UNAVAILABLE_MESSAGE


def production_attendance_available():
    from app.db import engine
    from app.fingerprint_bounded_parse import require_operational_parser_platform
    try:
        require_operational_parser_platform()
    except FingerprintPlatformUnavailable:
        return False
    return engine.dialect.name == 'postgresql'


def shift_day_router(templates):
    router=APIRouter()
    def identity(request,admin):return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def canonical_operational_path(request,comparison_id,employee_id,suffix):
        if (re.fullmatch(r'[0-9a-f]{64}',comparison_id) is None or type(employee_id) is not int or employee_id<1
            or request.scope.get('path')!=f'/attendance/shift-evidence/{comparison_id}/{employee_id}/{suffix}'):
            raise HTTPException(status_code=400,detail='Canonical source path required.',headers={'Cache-Control':'no-store'})
    def enabled():
        return settings.attendance_synthetic_review_enabled and (make_url(settings.database_url).database or '').startswith(('synthetic_attendance_','synthetic_cycle_')) and settings.app_env.strip().lower() in {'test','synthetic'}
    def render(request,admin,source,day,patterns,result=None,selected=(),review=None,registered=None):
        response=templates.TemplateResponse(request=request,name='shift_day_review.html',context=dict(admin=admin.username,csrf_token=get_csrf_token(request),source=source,day=day,patterns=patterns,result=result,selected=selected,operational=enabled() or production_attendance_available(),synthetic_registration=enabled(),operation_id=secrets.token_hex(32),review=review,registered=registered))
        response.headers['Cache-Control']='no-store';return response
    def config(db,source):
        employee=db.get(Employee,source.employee_id,populate_existing=True)
        patterns=db.scalars(select(ShiftPattern).where(ShiftPattern.department_id==employee.department_id,ShiftPattern.active.is_(True)).order_by(ShiftPattern.code).limit(101)).all() if employee else []
        if len(patterns)>100:raise ValueError('Configuration exceeds bounded window choices.')
        return patterns
    @router.get('/attendance/shift-evidence/{comparison_id}/{employee_id}')
    def page(request:Request,comparison_id:str,employee_id:int,work_date:date|None=None,admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                source=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**identity(request,admin))
                if work_date is not None and not any(v['date']==work_date.isoformat() for v in source.days):raise ValueError('Day outside saved source.')
                return render(request,admin,source,work_date,config(db,source))
        except (ValueError,TypeError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='Saved source unavailable.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/attendance/shift-evidence/{comparison_id}/{employee_id}',dependencies=[Depends(csrf_protect)])
    async def preview(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            if set(form)!={'csrf_token','work_date','windows'} or any(len(form.getlist(k))!=1 for k in ('csrf_token','work_date')):raise ValueError('Exact preview fields required.')
            raw_day=str(form['work_date'])
            if re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',raw_day) is None:raise ValueError('Explicit date required.')
            day=date.fromisoformat(raw_day);values=form.getlist('windows')
            if not 1<=len(values)<=100 or len(values)!=len(set(values)):raise ValueError('Explicit unique configured windows required.')
            windows=[]
            for value in values:
                if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{1,10}',str(value)) is None:raise ValueError('Exact configured selection required.')
                d,i=str(value).rsplit(':',1);windows.append((date.fromisoformat(d),int(i)))
            auth=identity(request,admin);actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    source=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**auth)
                    if not any(v['date']==day.isoformat() for v in source.days):raise ValueError('Day outside saved source.')
                    result=preview_configured_shift_evidence(db,employee_id=employee_id,work_date=day,selected_windows=windows,
                        fingerprint_bytes=source.fingerprint_bytes,fingerprint_filename=source.filename_hint,**auth)
                    response=render(request,actor,source,day,config(db,source),result,values);db.rollback();return response
            return await run_in_threadpool(transaction)
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:raise HTTPException(status_code=400,detail='Preview unavailable. Recheck selected source, day and current configuration.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/attendance/shift-evidence/{comparison_id}/{employee_id}/review',dependencies=[Depends(csrf_protect)])
    async def prepare(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if not enabled() and not production_attendance_available():raise HTTPException(status_code=503,detail=PLATFORM_UNAVAILABLE_MESSAGE,headers={'Cache-Control':'no-store'})
        canonical_operational_path(request,comparison_id,employee_id,'review')
        try:
            form=await request.form()
            keys={'csrf_token','operation_id','work_date','windows','selected_window','in_ordinal','out_ordinal','reason'}
            if set(form)!=keys or any(len(form.getlist(k))!=1 for k in keys-{'windows'}):raise ValueError('Exact review fields required.')
            if len(form.getlist('windows'))>100 or any(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{1,10}',str(v)) is None for v in form.getlist('windows')):raise ValueError('Exact configured selections required.')
            if any(re.fullmatch(r'[0-9]{1,6}',str(form[k])) is None for k in ('in_ordinal','out_ordinal')):raise ValueError('Explicit raw ordinals required.')
            day=date.fromisoformat(str(form['work_date']))
            windows=tuple((date.fromisoformat(d),int(i)) for d,i in (str(v).rsplit(':',1) for v in form.getlist('windows')))
            auth=identity(request,admin);actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    row=prepare_evidence_review(db,operation_id=str(form['operation_id']),comparison_id=comparison_id,employee_id=employee_id,work_date=day,
                        windows=windows,selected_window=str(form['selected_window']),in_ordinal=int(form['in_ordinal']),out_ordinal=int(form['out_ordinal']),reason=str(form['reason']),**auth)
                    source=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**auth)
                    evidence=json.loads(row.evidence_json);proposal=json.loads(row.proposal_json)
                    summary=dict(receipt_id=row.id,request_digest=row.request_digest,proposal=proposal,evidence=evidence)
                    response=render(request,actor,source,day,config(db,source),review=summary)
                    db.commit();return response
            return await run_in_threadpool(transaction)
        except FingerprintPlatformUnavailable as exc:
            raise HTTPException(status_code=503,detail=PLATFORM_UNAVAILABLE_MESSAGE,headers={'Cache-Control':'no-store'}) from exc
        except OperationalError as exc:
            if getattr(exc.orig,'sqlstate',None)=='55P03':
                raise HTTPException(status_code=503,detail='Source or restore gate busy; retry from current review.',headers={'Cache-Control':'no-store','Retry-After':'3'}) from exc
            raise HTTPException(status_code=400,detail='Database operation unavailable; no registration completed.',headers={'Cache-Control':'no-store'}) from exc
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:raise HTTPException(status_code=400,detail='Review unavailable. Recheck current month, service, source and selections.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/attendance/shift-evidence/{comparison_id}/{employee_id}/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,comparison_id:str,employee_id:int,admin:AdminAccount=Depends(require_system_admin)):
        if not enabled() and not production_attendance_available():raise HTTPException(status_code=503,detail=PLATFORM_UNAVAILABLE_MESSAGE,headers={'Cache-Control':'no-store'})
        canonical_operational_path(request,comparison_id,employee_id,'confirm')
        try:
            form=await request.form()
            if set(form)!={'csrf_token','receipt_id','request_digest','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form) or form['human_confirmed']!='yes':raise ValueError('Exact explicit confirmation required.')
            auth=identity(request,admin);actor=SimpleNamespace(username=admin.username)
            def transaction():
                with SessionLocal() as db:
                    record=consume_evidence_review(db,receipt_id=str(form['receipt_id']),confirmed_request_digest=str(form['request_digest']),route_comparison_id=comparison_id,route_employee_id=employee_id,**auth)
                    source=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**auth)
                    response=render(request,actor,source,record.work_date,config(db,source),registered=record.id)
                    db.commit();return response
            return await run_in_threadpool(transaction)
        except FingerprintPlatformUnavailable as exc:
            raise HTTPException(status_code=503,detail=PLATFORM_UNAVAILABLE_MESSAGE,headers={'Cache-Control':'no-store'}) from exc
        except OperationalError as exc:
            if getattr(exc.orig,'sqlstate',None)=='55P03':
                raise HTTPException(status_code=503,detail='Source or restore gate busy; retry from current review.',headers={'Cache-Control':'no-store','Retry-After':'3'}) from exc
            raise HTTPException(status_code=400,detail='Database operation unavailable; no registration completed.',headers={'Cache-Control':'no-store'}) from exc
        except (ValueError,TypeError,KeyError,SQLAlchemyError) as exc:raise HTTPException(status_code=400,detail='Registration unavailable. Review may be stale or source already used. No overwrite.',headers={'Cache-Control':'no-store'}) from exc
    templates.env.globals['shift_adjacent_days']=lambda day:(day-timedelta(days=1),day,day+timedelta(days=1))
    return router
