# ruff: noqa: B008
"""HR policy field preview and version save. No recalculation or activation."""
from dataclasses import asdict
from hashlib import sha256
import re
from fastapi import APIRouter,Depends,Request,HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.models import AdminAccount,LeavePolicyVersion
from app.leave_policy_store import read_policy_version,canonical_policy,policy_read_guard
from app.leave_policy_edit import prepare_policy_edit,read_policy_edit,confirm_policy_edit


def policy_editor_router(templates):
    router=APIRouter()
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)

    @router.get('/leave/policies')
    def page(request:Request,base_id:str='',draft_id:str='',admin:AdminAccount=Depends(require_system_admin)):
        values=None;draft=None;before=None;versions=[]
        try:
            with SessionLocal() as db:
                policy_read_guard(db,**identity(request,admin))
                rows=db.execute(select(LeavePolicyVersion.id,LeavePolicyVersion.created_at).where(
                    LeavePolicyVersion.restore_epoch.is_(None) if request.state.restore_epoch is None else LeavePolicyVersion.restore_epoch==request.state.restore_epoch)
                    .order_by(LeavePolicyVersion.created_at.desc(),LeavePolicyVersion.id).limit(30)).all()
                for row in rows:
                    try:read_policy_version(db,policy_id=row.id,**identity(request,admin))
                    except ValueError:continue
                    versions.append(dict(id=row.id,time=row.created_at.isoformat(timespec='seconds')))
                if base_id:
                    values=asdict(read_policy_version(db,policy_id=base_id,**identity(request,admin)))
                if draft_id:
                    row,old,new=read_policy_edit(db,draft_id=draft_id,**identity(request,admin))
                    draft=dict(id=row.id,digest=row.review_sha256,reason=row.reason,saved_policy_id=row.saved_policy_id)
                    before=asdict(old);values=asdict(new);base_id=row.base_policy_id
                    digest=sha256(canonical_policy(new).encode()).hexdigest()
                    matching=db.scalar(select(LeavePolicyVersion.id).where(LeavePolicyVersion.policy_sha256==digest,
                        LeavePolicyVersion.restore_epoch.is_(None) if request.state.restore_epoch is None else LeavePolicyVersion.restore_epoch==request.state.restore_epoch))
                    if matching:
                        read_policy_version(db,policy_id=matching,**identity(request,admin))
                        draft['matching_policy_id']=matching
            return templates.TemplateResponse(request=request,name='leave_policies.html',context=dict(admin=admin.username,
                csrf_token=get_csrf_token(request),versions=versions,values=values,before=before,draft=draft,base_id=base_id,
                error=request.query_params.get('error')=='1'))
        except (ValueError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='Not found') from exc

    @router.post('/leave/policies/preview',dependencies=[Depends(csrf_protect)])
    async def preview(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            allowed={'csrf_token','base_id','reason','tier30_by_insurance_years','tier30_by_age_50','annual_deduct_unpaid_absence',
                'annual_deduct_sick','annual_year_basis','cash_basis','holiday_credit_during_sick','sick_alert_thresholds'}
            if set(form)!=allowed or any(len(form.getlist(k))!=1 for k in allowed):raise ValueError('Exact unique policy fields required.')
            base_id=str(form['base_id'])
            if re.fullmatch('[0-9a-f]{64}',base_id) is None:raise ValueError('Exact policy reference required.')
            with SessionLocal() as db,db.begin():
                row=prepare_policy_edit(db,base_policy_id=base_id,fields={k:str(form[k]) for k in allowed-{'csrf_token','base_id','reason'}},
                    reason=str(form['reason']),**identity(request,admin))
                draft_id=row.id
            return RedirectResponse('/leave/policies?draft_id='+draft_id,status_code=303)
        except (ValueError,SQLAlchemyError):return RedirectResponse('/leave/policies?error=1',status_code=303)

    @router.post('/leave/policies/confirm',dependencies=[Depends(csrf_protect)])
    async def confirm(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            if set(form)!={'csrf_token','draft_id','reviewed_sha256','human_confirmed'} or any(len(form.getlist(k))!=1 for k in form):raise ValueError('Exact confirmation fields required.')
            draft_id=str(form['draft_id'])
            if re.fullmatch('[0-9a-f]{64}',draft_id) is None:raise ValueError('Exact policy draft reference required.')
            with SessionLocal() as db,db.begin():
                confirm_policy_edit(db,draft_id=draft_id,reviewed_sha256=str(form['reviewed_sha256']),
                    human_confirmed=form['human_confirmed']=='confirmed',**identity(request,admin))
            return RedirectResponse('/leave/policies?draft_id='+draft_id,status_code=303)
        except (ValueError,SQLAlchemyError):return RedirectResponse('/leave/policies?error=1',status_code=303)
    return router
