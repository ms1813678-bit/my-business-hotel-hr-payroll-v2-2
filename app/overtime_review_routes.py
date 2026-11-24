"""Owner-selected OT day-list/detail preparation, no award or payroll effects."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from app.csrf import get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_payroll_access
from app.models import AdminAccount, AttendanceDailyRecord, Employee


def overtime_preparation_router(templates):
    router = APIRouter()

    @router.get('/overtime/review')
    def overtime_days(request: Request, employee_id: int | None = None,
                      work_date: date | None = None,
                      admin: AdminAccount = Depends(require_payroll_access)):  # noqa: B008
        # Bounded persisted read. No default month, employee or missing zero values.
        with SessionLocal() as db, db.no_autoflush:
            employees = db.execute(select(Employee.id, Employee.code, Employee.full_name)
                .order_by(Employee.code).limit(500)).all()
            employee = None
            rows = []
            selected = None
            if employee_id is not None:
                employee = db.execute(select(Employee.id, Employee.code, Employee.full_name)
                    .where(Employee.id == employee_id)).first()
                if employee is not None:
                    rows = db.execute(select(AttendanceDailyRecord.work_date,
                        AttendanceDailyRecord.worked_minutes,
                        AttendanceDailyRecord.scheduled_start_at,
                        AttendanceDailyRecord.scheduled_end_at,
                        AttendanceDailyRecord.status).where(
                        AttendanceDailyRecord.employee_id == employee_id
                        ).order_by(AttendanceDailyRecord.work_date.desc()).limit(62)).all()
                    if work_date is not None:
                        selected = db.execute(select(AttendanceDailyRecord.work_date,
                            AttendanceDailyRecord.worked_minutes,
                            AttendanceDailyRecord.scheduled_start_at,
                            AttendanceDailyRecord.scheduled_end_at,
                            AttendanceDailyRecord.status).where(
                            AttendanceDailyRecord.employee_id == employee_id,
                            AttendanceDailyRecord.work_date == work_date)).first()
            return templates.TemplateResponse(request=request, name='overtime_review.html', context={
                'admin': admin.display_name or admin.username, 'employees': employees,
                'employee': employee, 'rows': rows, 'selected': selected,
                'requested_day': work_date, 'csrf_token': get_csrf_token(request)})

    return router
