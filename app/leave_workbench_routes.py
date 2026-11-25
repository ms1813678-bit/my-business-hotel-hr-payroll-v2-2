# ruff: noqa: B008
"""Employee-first leave workbench. Read foundation, no approval or posting."""
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.csrf import get_csrf_token
from app.db import SessionLocal, engine
from app.dependencies import require_attendance_write, require_leave_read
from app.leave_exact_read import exact_ledger_balance
from app.leave_exact_amount import display_days
from app.leave_register_receipts import (
    confirm_register_receipt,
    inspect_register_receipt,
    issue_register_receipt,
)
from app.leave_register_review import RegisterEdit
from app.models import AdminAccount, Employee, LeaveRegisterEntry, LeaveType


def leave_workbench_router(templates):
    router = APIRouter()

    @router.get('/leave/workbench')
    def employee_workbench(request: Request, employee_id: int | None = None,
                            period_start: date | None = None, period_end: date | None = None,
                            admin: AdminAccount = Depends(require_leave_read)):
        with SessionLocal() as db, db.no_autoflush:
            employees = db.execute(select(Employee.id, Employee.code, Employee.full_name)
                .order_by(Employee.code).limit(500)).all()
            employee = None
            rows, balances = [], []
            valid_period = (period_start is not None and period_end is not None
                            and period_start <= period_end
                            and (period_end - period_start).days <= 365)
            if employee_id is not None:
                employee = db.execute(select(Employee.id, Employee.code, Employee.full_name)
                    .where(Employee.id == employee_id)).first()
                if employee is not None:
                    if admin.role in {'system_admin', 'finance'}:
                        for kind in db.scalars(select(LeaveType).where(LeaveType.tracks_balance.is_(True)
                                ).order_by(LeaveType.id)).all():
                            balances.append((kind.name_ar, kind.name_en, kind.name_zh,
                                display_days(exact_ledger_balance(db, employee.id, kind.id))))
                    # Finance reads balances, not the assistant's day-entry workspace.
                    if admin.role in {'system_admin', 'assistant_manager'} and valid_period:
                        rows = db.execute(select(LeaveRegisterEntry.work_date, LeaveRegisterEntry.code,
                            LeaveRegisterEntry.notes, LeaveRegisterEntry.revision).where(
                            LeaveRegisterEntry.employee_id == employee_id,
                            LeaveRegisterEntry.work_date >= period_start,
                            LeaveRegisterEntry.work_date <= period_end
                            ).order_by(LeaveRegisterEntry.work_date)).all()
            return templates.TemplateResponse(request=request, name='leave_workbench.html', context={
                'admin': admin.display_name or admin.username, 'role': admin.role,
                'employees': employees, 'employee': employee, 'rows': rows,
                'balances': balances, 'period_start': period_start, 'period_end': period_end,
                'valid_period': valid_period, 'csrf_token': get_csrf_token(request)})

    def identity(request, admin):
        return {'actor_id': admin.id, 'session_id': request.session.get('admin_session_id'),
                'session_version': request.session.get('admin_session_version')}

    def review_response(request, admin, **values):
        return templates.TemplateResponse(request=request, name='leave_day_review.html', context={
            'admin': admin.display_name or admin.username, 'csrf_token': get_csrf_token(request),
            'token': None, 'edit': None, 'review': None, 'error': False, 'saved': None, **values})

    # Candidate manual review flow retained but NOT registered after input-scope correction.
    def review_day(request: Request, employee_id: int = Form(...),
                   work_date: date = Form(...), period_start: date = Form(...), period_end: date = Form(...),
                   day_code: str = Form(...), certificate: str = Form('unknown'), notes: str = Form(''),
                   admin: AdminAccount = Depends(require_attendance_write)):
        codes = {'unknown': None, 'present': '', 'U': 'U', 'A': 'A', 'S': 'S',
                 'H': 'H', 'W': 'W', 'outside': '—'}
        certificates = {'unknown': None, 'yes': True, 'no': False}
        if day_code not in codes or certificate not in certificates:
            raise HTTPException(status_code=422, detail='البيانات غير مكتملة أو غير صحيحة')
        edit = RegisterEdit(employee_id, work_date, codes[day_code], certificates[certificate],
                            notes or None, 'manual_review', period_start, period_end)
        try:
            token, review = issue_register_receipt(engine, edit=edit, **identity(request, admin))
            return review_response(request, admin, token=token, edit=edit, review=review)
        except (ValueError, SQLAlchemyError):
            return review_response(request, admin, error=True)

    # Candidate confirmation retained but NOT registered.
    def confirm_day(request: Request, receipt: str = Form(...), human_confirmed: str = Form(''),
                    admin: AdminAccount = Depends(require_attendance_write)):
        try:
            edit, _snapshot, _digest = inspect_register_receipt(engine, token=receipt, **identity(request, admin))
            result = confirm_register_receipt(engine, token=receipt, **identity(request, admin),
                                              human_confirmed=human_confirmed == 'confirmed')
            return review_response(request, admin, saved=result, edit=edit)
        except (ValueError, PermissionError, SQLAlchemyError):
            # Generic localized refusal does not expose raw PG/trigger messages.
            # Re-review is explicit, never reuse the old receipt for new values.
            return review_response(request, admin, error=True)

    return router
