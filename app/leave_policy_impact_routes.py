# ruff: noqa: B008
"""Explicit earned-only hypothetical policy impact, no save/commit action."""
from datetime import date
from decimal import Decimal,localcontext
import re
from fastapi import APIRouter,Depends,Request,HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select,func,and_
from sqlalchemy.exc import SQLAlchemyError
from app.csrf import csrf_protect,get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.leave_policy_edit import read_policy_edit
from app.leave_policy_store import policy_read_guard
from app.leave_policy_impact import preview_earned_policy_impact
from app.leave_opening_source import read_confirmed_opening_source
from app.models import AdminAccount,Department,Employee,LeaveOpeningSelection


def policy_impact_router(templates):
    router=APIRouter()
    def display_days(value):
        with localcontext() as context:
            context.prec=4096
            return format(value,'.2f')
    templates.env.filters['impact_days']=display_days
    def parse_department(raw):
        if not isinstance(raw,str) or re.fullmatch(r'[1-9][0-9]{0,9}',raw) is None or int(raw)>2147483647:
            raise ValueError('Canonical positive department required.')
        return int(raw)
    def parse_month(raw):
        if not isinstance(raw,str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}',raw) is None:raise ValueError('Canonical month required.')
        value=date.fromisoformat(raw+'-01')
        if value.strftime('%Y-%m')!=raw:raise ValueError('Canonical month required.')
        return value
    def parse_day(raw):
        if not isinstance(raw,str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',raw) is None:raise ValueError('Canonical calendar date required.')
        value=date.fromisoformat(raw)
        if value.isoformat()!=raw:raise ValueError('Canonical calendar date required.')
        return value
    def identity(request,admin):
        return dict(actor_id=admin.id,session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
    def render(request,admin,db,draft_id,department_id,month,result=None,submitted=None):
        draft,_,_=read_policy_edit(db,draft_id=draft_id,**identity(request,admin))
        target=parse_month(month) if month else None
        months=[date(target.year,m,1) for m in range(1,target.month+1)] if target else []
        departments=db.execute(select(Department.id,Department.name).order_by(Department.name)).all()
        choices=[]
        if department_id and target:
            epoch=request.state.restore_epoch
            latest=select(LeaveOpeningSelection.employee_id.label('employee_id'),func.max(LeaveOpeningSelection.revision).label('revision')).where(
                LeaveOpeningSelection.restore_epoch.is_(None) if epoch is None else LeaveOpeningSelection.restore_epoch==epoch).group_by(LeaveOpeningSelection.employee_id).subquery()
            candidates=db.execute(select(LeaveOpeningSelection.id,Employee.id.label('employee_id'),Employee.code,Employee.full_name)
                .join(Employee,Employee.id==LeaveOpeningSelection.employee_id).join(latest,and_(latest.c.employee_id==LeaveOpeningSelection.employee_id,latest.c.revision==LeaveOpeningSelection.revision))
                .where(Employee.department_id==department_id,LeaveOpeningSelection.policy_id==draft.base_policy_id,LeaveOpeningSelection.restore_epoch.is_(None) if epoch is None else LeaveOpeningSelection.restore_epoch==epoch)
                .order_by(Employee.code,LeaveOpeningSelection.revision.desc()).limit(301)).all()
            if len(candidates)>300:raise ValueError('Narrow source selection required.')
            for candidate in candidates:
                try:read_confirmed_opening_source(db,selection_id=candidate.id,**identity(request,admin))
                except ValueError:continue
                choices.append(candidate)
        policy_read_guard(db,**identity(request,admin))
        result_employees={}
        if result:
            result_employees={row.id:row for row in db.execute(select(Employee.id,Employee.code,Employee.full_name).where(Employee.id.in_({r.employee_id for r in result.rows}))).all()}
        if result and set(result_employees)!={r.employee_id for r in result.rows}:raise ValueError('Selected employee source unavailable.')
        response=templates.TemplateResponse(request=request,name='leave_policy_impact.html',context=dict(
            admin=admin.username,csrf_token=get_csrf_token(request),draft=draft,departments=departments,
            department_id=department_id,month=month,months=months,choices=choices,result=result,submitted=submitted,result_employees=result_employees))
        response.headers['Cache-Control']='no-store'
        return response
    @router.get('/leave/policies/impact')
    def page(request:Request,draft_id:str,department_id:str='',month:str='',admin:AdminAccount=Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                return render(request,admin,db,draft_id,parse_department(department_id) if department_id else None,month)
        except (ValueError,SQLAlchemyError) as exc:raise HTTPException(status_code=404,detail='تعذر عرض المعاينة. راجع مرجع المسودة والقسم والشهر وصلاحية الجلسة. قد تتجاوز المصادر حد المعاينة المحلية.',headers={'Cache-Control':'no-store'}) from exc
    @router.post('/leave/policies/impact',dependencies=[Depends(csrf_protect)])
    async def preview(request:Request,admin:AdminAccount=Depends(require_system_admin)):
        try:
            form=await request.form()
            fixed={'csrf_token','draft_id','reviewed_sha256','department_id','month','selection_ids'}
            if not fixed<=set(form):raise ValueError('Complete impact fields required.')
            selected=tuple(form.getlist('selection_ids'))
            if not 1<=len(selected)<=300 or len(set(selected))!=len(selected) or any(re.fullmatch('[0-9a-f]{64}',i) is None for i in selected):raise ValueError('Exact selected sources required.')
            draft_id=str(form['draft_id']);month=str(form['month']);department_id=parse_department(str(form['department_id']))
            target=parse_month(month);months=[date(target.year,m,1) for m in range(1,target.month+1)]
            ages={m:parse_day(str(form['age_'+m.isoformat()])) for m in months}
            insurance={};expected=fixed|{'age_'+m.isoformat() for m in months}
            with SessionLocal() as db:
                from app.department_workbook_store import _guard
                _guard(db,**identity(request,admin))
                for selected_id in selected:
                    reviews,_,_=read_confirmed_opening_source(db,selection_id=selected_id,**identity(request,admin))
                    employee_id=reviews[0]['employee_id']
                    for m in months:
                        key='insurance_'+str(employee_id)+'_'+m.isoformat();expected.add(key)
                        raw=str(form.get(key,''))
                        if raw and re.fullmatch(r'[0-9]{1,60}(?:\.[0-9]{1,30})?',raw) is None:raise ValueError('Bounded explicit insurance required.')
                        insurance[employee_id,m]=Decimal(raw) if raw else None
                # Unselected displayed employee insuranceinputs are intentionally
                # not accepted: each preview applies only to checked employee rows.
                if set(form)!=expected or any(len(form.getlist(k))!=1 for k in expected-{'selection_ids'}):raise ValueError('Exact unique impact evidence required.')
                result=preview_earned_policy_impact(db,draft_id=draft_id,reviewed_sha256=str(form['reviewed_sha256']),selection_ids=selected,
                    department_id=department_id,month_start=target,current_insurance_years=insurance,age_evaluation_dates=ages,**identity(request,admin))
                response=render(request,admin,db,draft_id,department_id,month,result,dict(form))
                db.rollback()
            response.headers['Cache-Control']='no-store'
            return response
        except (ValueError,KeyError,TypeError,SQLAlchemyError):return RedirectResponse('/leave/policies?error=1',status_code=303,headers={'Cache-Control':'no-store'})
    return router
