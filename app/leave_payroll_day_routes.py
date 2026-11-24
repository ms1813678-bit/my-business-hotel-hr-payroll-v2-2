"""Read-only factual day handoff. No source-selection/ledger permission expansion."""
from fastapi import APIRouter,Depends,Request,HTTPException
from sqlalchemy.exc import SQLAlchemyError
from app.dependencies import require_payroll_access
from app.models import AdminAccount
from app.db import SessionLocal
from app.csrf import get_csrf_token
from app.leave_payroll_days import read_payroll_days
from app.leave_month_review_routes import month_value


def payroll_day_handoff_router(templates):
    router=APIRouter()
    @router.get('/leave/payroll-day-handoff')
    def page(request:Request,employee_id:int,month:str='',year:str='',month_number:str='',admin:AdminAccount=Depends(require_payroll_access)):
        try:
            if year or month_number:
                if month:raise ValueError('One exact period required.')
                month=year+'-'+month_number
            target=month_value(month) if month else None
            result=None
            if target:
                with SessionLocal() as db:
                    result=read_payroll_days(db,employee_id=employee_id,year=target.year,month=target.month,actor_id=admin.id,
                        session_id=request.session.get('admin_session_id'),session_version=request.session.get('admin_session_version'),expected_epoch=request.state.restore_epoch)
            response=templates.TemplateResponse(request=request,name='leave_payroll_day_handoff.html',context=dict(
                admin=admin.username,employee_id=employee_id,month=month,result=result,csrf_token=get_csrf_token(request),
                month_names=('يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر')))
            response.headers['Cache-Control']='no-store';return response
        except (ValueError,TypeError,SQLAlchemyError) as exc:
            busy=getattr(getattr(exc,'orig',None),'sqlstate',None)=='55P03'
            raise HTTPException(503 if busy else 400,'تعذر عرض تسليم الأيام. راجع الموظف والفترة والجلسة.',headers={'Cache-Control':'no-store',**({'Retry-After':'3'} if busy else {})}) from exc
    return router
