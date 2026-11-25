# ruff: noqa: B008
"""Selected manual opening UI, HR-only preparation/review, no apply endpoint."""
from datetime import date
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.leave_manual_opening_groups import persist_manual_opening_group, read_manual_opening_group
from app.models import AdminAccount, Employee, LeaveManualOpeningGroup, LeavePolicyVersion
from app.leave_policy_store import OWNER_DEFAULT_POLICY, persist_policy_version, read_policy_version
from app.leave_opening_source import review_opening_source, confirm_opening_source


def manual_opening_router(templates):
    router = APIRouter()

    def identity(request, admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)

    def render(request, admin, *, error=False, reviews=(), employee=None, group_id=None, source_review=None, apply_review=None, apply_receipt=None, selection_id=None):
        with SessionLocal() as db, db.no_autoflush:
            employees = db.execute(select(Employee.id, Employee.code, Employee.full_name).order_by(Employee.code)).all()
            recent_groups = db.execute(select(LeaveManualOpeningGroup.id, LeaveManualOpeningGroup.created_at)
                .where(LeaveManualOpeningGroup.restore_epoch.is_(None) if request.state.restore_epoch is None
                    else LeaveManualOpeningGroup.restore_epoch == request.state.restore_epoch)
                .order_by(LeaveManualOpeningGroup.created_at.desc(), LeaveManualOpeningGroup.id).limit(20)).all()
            employee_labels = {e.id: f'{e.code} · {e.full_name}' for e in employees}
            validated_groups = []
            for group in recent_groups:
                try:
                    selected = read_manual_opening_group(db, group_id=group.id, actor_id=admin.id,
                        session_id=request.session.get('admin_session_id'),
                        session_version=request.session.get('admin_session_version'),
                        expected_epoch=request.state.restore_epoch)
                except ValueError:
                    continue  # Never present stale/tampered proposals as usable choices.
                validated_groups.append(dict(id=group.id,
                    saved_at=group.created_at.astimezone(ZoneInfo('Africa/Cairo')).strftime('%Y-%m-%d %H:%M'),
                    employee=employee_labels.get(selected[0]['employee_id'], ''),
                    basis=selected[0]['effective_start']))
            recent_groups = validated_groups
            policies = db.execute(select(LeavePolicyVersion.id,LeavePolicyVersion.created_at)
                .where(LeavePolicyVersion.restore_epoch.is_(None) if request.state.restore_epoch is None
                    else LeavePolicyVersion.restore_epoch==request.state.restore_epoch)
                .order_by(LeavePolicyVersion.created_at.desc()).limit(30)).all()
            policy_choices=[]
            for policy in policies:
                try:
                    value=read_policy_version(db,policy_id=policy.id,**identity(request,admin))
                    if value==OWNER_DEFAULT_POLICY:
                        policy_choices.append(dict(id=policy.id,created_at=policy.created_at))
                except ValueError:
                    continue
        return templates.TemplateResponse(request=request, name='leave_manual_opening.html', context={
            'admin': admin.display_name or admin.username, 'role': admin.role,
            'employees': employees, 'csrf_token': get_csrf_token(request),
            'error': error, 'reviews': reviews, 'selected_employee': employee, 'recent_groups': recent_groups, 'policies':policy_choices, 'group_id':group_id,
            'source_review':source_review, 'policy_id':request.query_params.get('policy_id'),
            'apply_review':apply_review,'apply_receipt':apply_receipt,'selection_id':selection_id,
            'confirmed':bool(source_review and source_review['is_current'])})

    @router.get('/leave/opening')
    def opening_form(request: Request, admin: AdminAccount = Depends(require_system_admin)):
        return render(request, admin, error=request.query_params.get('error') == '1')

    @router.get('/leave/opening/groups/{group_id}')
    def saved_group(request: Request, group_id: str, admin: AdminAccount = Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                reviews = read_manual_opening_group(db, group_id=group_id, actor_id=admin.id,
                    session_id=request.session.get('admin_session_id'),
                    session_version=request.session.get('admin_session_version'),
                    expected_epoch=request.state.restore_epoch)
                employee = db.execute(select(Employee.code, Employee.full_name).where(Employee.id == reviews[0]['employee_id'])).first()
                policy_id=request.query_params.get('policy_id')
                source_review=review_opening_source(db,group_id=group_id,policy_id=policy_id,**identity(request,admin)) if policy_id else None
                if source_review and source_review['policy']!=OWNER_DEFAULT_POLICY:
                    raise ValueError('This review screen supports the displayed policy version only.')
                apply_review = apply_receipt = selection_id = None
                if source_review and source_review['is_current']:
                    from app.models import LeaveOpeningSelection, LeaveType
                    from app.leave_opening_apply import CANONICAL_TYPES, review_opening_apply, _receipt, _stored
                    selection = db.scalar(select(LeaveOpeningSelection).where(
                        LeaveOpeningSelection.group_id==group_id,LeaveOpeningSelection.policy_id==policy_id)
                        .order_by(LeaveOpeningSelection.revision.desc()).limit(1))
                    if selection is not None:
                        selection_id=selection.id
                        type_map={c:db.scalar(select(LeaveType.id).where(LeaveType.code==k)) for c,k in CANONICAL_TYPES.items()}
                        previous=_receipt(db,selection.id)
                        if previous is not None:
                            apply_receipt=_stored(db,previous,type_map)
                        else:
                            try:
                                apply_review=review_opening_apply(db,selection_id=selection.id,type_map=type_map,**identity(request,admin))
                            except ValueError:
                                apply_review={'blocked':True}
            return render(request, admin, reviews=reviews, employee=employee,group_id=group_id,source_review=source_review,
                          apply_review=apply_review,apply_receipt=apply_receipt,selection_id=selection_id)
        except (ValueError, SQLAlchemyError) as exc:
            raise HTTPException(status_code=404, detail='Not found') from exc

    @router.post('/leave/opening/review', dependencies=[Depends(csrf_protect)])
    def opening_review(request: Request, employee_id: int = Form(...),
                       effective_start: date = Form(...), annual: str = Form(...),
                       weekly: str = Form(...), holiday: str = Form(...), reason: str = Form(...),
                       admin: AdminAccount = Depends(require_system_admin)):
        try:
            # Bound before Decimal construction, no floats/locale coercion.
            import re
            if not reason.strip() or len(reason)>1000:
                raise ValueError('Bounded review reason required.')
            values = (annual, weekly, holiday)
            if any(len(v) > 200 or re.fullmatch(r'\d+(?:\.\d+)?', v) is None for v in values):
                raise ValueError('Exact nonnegative decimal text required.')
            with SessionLocal() as db, db.begin():
                group, _reviews = persist_manual_opening_group(db, employee_id=employee_id,
                    balances=dict(zip(('A', 'W', 'H'), map(Decimal, values), strict=True)),
                    effective_start=effective_start, reason=reason, actor_id=admin.id,
                    session_id=request.session.get('admin_session_id'),
                    session_version=request.session.get('admin_session_version'),
                    expected_epoch=request.state.restore_epoch)
                group_id = group.id
            return RedirectResponse('/leave/opening/groups/' + group_id, status_code=303)
        except (ValueError, InvalidOperation, SQLAlchemyError):
            # No DB reads/rendering in the exempt POST, including invalid input
            # and failed transactions. Fresh auth + shared gate on GET.
            return RedirectResponse('/leave/opening?error=1', status_code=303)


    @router.post('/leave/opening/policy-defaults', dependencies=[Depends(csrf_protect)])
    def initialize_policy(request: Request, group_id: str=Form(...),
                          admin: AdminAccount=Depends(require_system_admin)):
        try:
            import re
            if any(re.fullmatch('[0-9a-f]{64}',value) is None for value in (group_id,)):
                raise ValueError('Exact stored reference required.')
            with SessionLocal() as db, db.begin():
                from app.department_workbook_store import _guard
                _guard(db,**identity(request,admin))
                read_manual_opening_group(db,group_id=group_id,**identity(request,admin))
                row=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,
                    reason='Explicit initialization of owner-defined leave defaults',**identity(request,admin))
                policy_id=row.id
            return RedirectResponse('/leave/opening/groups/'+group_id+'?policy_id='+policy_id,status_code=303)
        except (ValueError,SQLAlchemyError):
            return RedirectResponse('/leave/opening?error=1',status_code=303)

    @router.post('/leave/opening/confirm-source', dependencies=[Depends(csrf_protect)])
    async def confirm_source(request: Request, group_id: str=Form(...),policy_id: str=Form(...),
                       source_sha256: str=Form(''), previous_id: str=Form(''),
                       operation_stage: str=Form('confirm_source'), selection_id: str=Form(''),
                       response_format: str=Form('json'),
                       reviewed_apply_sha256: str=Form(''),
                       reason: str=Form(...),human_confirmed: str=Form(''),
                       admin: AdminAccount=Depends(require_system_admin)):
        from starlette.concurrency import run_in_threadpool
        parsed_form = await request.form()
        def execute():
            try:
                import re
                if operation_stage in ('review_apply', 'apply'):
                    form = parsed_form
                    allowed = {'csrf_token','group_id','policy_id','selection_id','operation_stage',
                               'source_sha256','previous_id','reviewed_apply_sha256','reason','human_confirmed','response_format'}
                    if any(k not in allowed or len(form.getlist(k)) != 1 for k in form):
                        raise ValueError('Exact opening operation fields required.')
                    if any(re.fullmatch('[0-9a-f]{64}',v) is None for v in (group_id,policy_id,selection_id)):
                        raise ValueError('Exact opening references required.')
                    from app.leave_opening_apply import apply_confirmed_opening, review_opening_apply, CANONICAL_TYPES
                    from app.models import LeaveOpeningSelection, LeaveType
                    with SessionLocal() as db, db.begin():
                        from app.department_workbook_store import _guard
                        _guard(db, **identity(request,admin))
                        selection = db.get(LeaveOpeningSelection, selection_id, populate_existing=True)
                        if selection is None or selection.group_id != group_id or selection.policy_id != policy_id:
                            raise ValueError('Exact confirmed selection required.')
                        # Model supplies no type ids. Canonical source codes alone select them.
                        type_map = {c:db.scalar(select(LeaveType.id).where(LeaveType.code == k)) for c,k in CANONICAL_TYPES.items()}
                        if operation_stage == 'review_apply':
                            result = review_opening_apply(db,selection_id=selection_id,type_map=type_map,**identity(request,admin))
                        else:
                            result = apply_confirmed_opening(db,selection_id=selection_id,type_map=type_map,
                                reviewed_apply_sha256=reviewed_apply_sha256,reason=reason,
                                human_confirmed=human_confirmed == 'confirmed',**identity(request,admin))
                    # No database reads or template render after the guarded transaction.
                    if response_format == 'html':
                        return RedirectResponse('/leave/opening/groups/'+group_id+'?policy_id='+policy_id,status_code=303)
                    return JSONResponse(result)
                if operation_stage != 'confirm_source':
                    raise ValueError('Unknown opening operation.')
                if any(re.fullmatch('[0-9a-f]{64}',value) is None for value in (group_id,policy_id,source_sha256)) or (previous_id and re.fullmatch('[0-9a-f]{64}',previous_id) is None):
                    raise ValueError('Exact stored source references required.')
                with SessionLocal() as db, db.begin():
                    from app.department_workbook_store import _guard
                    _guard(db,**identity(request,admin))
                    if read_policy_version(db,policy_id=policy_id,**identity(request,admin))!=OWNER_DEFAULT_POLICY:
                        raise ValueError('Displayed policy version required.')
                    confirm_opening_source(db,group_id=group_id,policy_id=policy_id,
                        reviewed_source_sha256=source_sha256,expected_previous_id=previous_id or None,
                        reason=reason,human_confirmed=human_confirmed=='confirmed',**identity(request,admin))
                return RedirectResponse('/leave/opening/groups/'+group_id+'?policy_id='+policy_id+'&confirmed=1',status_code=303)
            except (ValueError,SQLAlchemyError):
                return RedirectResponse('/leave/opening?error=1',status_code=303)
        return await run_in_threadpool(execute)

    return router
