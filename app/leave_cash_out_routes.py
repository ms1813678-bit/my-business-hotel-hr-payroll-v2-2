# ruff: noqa: B008
"""HR explicit manual leave cash-out: review GET/read, review POST, confirm POST.

Mirrors leave_month_earning_routes: signed/session-bound proof, exact form
field allowlists, real CSRF, system_admin only. The confirmation transaction
runs the exclusive-first credential/restore gate (_guard) BEFORE the service
takes any table lock. Finance/assistant get 403 from require_system_admin on
every route here; finance read visibility stays on the existing leave read
surface, unchanged.
"""
import re
import secrets
from datetime import date
from fractions import Fraction

from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.dependencies import require_system_admin
from app.leave_cash_out import apply_cash_out, parse_cash_days, review_cash_out
from app.leave_exact_amount import canonical_amount
from app.models import AdminAccount, Employee, LeaveType, PayrollItem, PayrollRun


def cash_out_router(templates):
    router = APIRouter()
    signer = URLSafeTimedSerializer(settings.session_secret, salt='leave-cash-out-v1')

    def identity(request, admin):
        return {'actor_id': admin.id, 'session_id': request.session.get('admin_session_id'),
                'session_version': request.session.get('admin_session_version'),
                'expected_epoch': request.state.restore_epoch}

    def render(request, actor, **values):
        context = {'admin': actor, 'csrf_token': get_csrf_token(request),
                   'review': None, 'proof': None, 'saved': None, 'reason': '',
                   'evaluated_on': '', 'days': '', 'leave_types': [], 'payroll_items': [],
                   'selected_type': None, 'selected_item': None}
        context.update(values)
        response = templates.TemplateResponse(request=request, name='leave_cash_out.html', context=context)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def fail(exc):
        code = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
        raise HTTPException(status_code=503 if code == '55P03' else 400,
            detail='تعذرت مراجعة صرف الإجازة. راجع المصدر والرصيد والجلسة.',
            headers={'Cache-Control': 'no-store', **({'Retry-After': '3'} if code == '55P03' else {})}) from exc

    def choices(db, employee_id):
        employee = db.get(Employee, employee_id, populate_existing=True)
        if employee is None:
            raise ValueError('Employee required.')
        types = db.scalars(select(LeaveType).where(LeaveType.active.is_(True),
            LeaveType.tracks_balance.is_(True)).order_by(LeaveType.id)).all()
        items = db.execute(select(PayrollItem, PayrollRun).join(
            PayrollRun, PayrollItem.payroll_run_id == PayrollRun.id).where(
            PayrollItem.employee_id == employee_id).order_by(PayrollRun.period_start.desc())).all()
        return employee, types, items

    root = '/leave/cash-out/{employee_id}'

    @router.get(root)
    def page(request: Request, employee_id: int, admin: AdminAccount = Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                employee, types, items = choices(db, employee_id)
                return render(request, admin.username, employee=employee,
                              leave_types=types, payroll_items=items)
        except (ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError) as exc:
            fail(exc)

    @router.post(root + '/review', dependencies=[Depends(csrf_protect)])
    async def review(request: Request, employee_id: int, admin: AdminAccount = Depends(require_system_admin)):
        if request.scope.get('path') != f'/leave/cash-out/{employee_id}/review':
            raise HTTPException(400, 'Canonical operation path required.')
        try:
            form = await request.form()
            if set(form) != {'csrf_token', 'leave_type_id', 'payroll_item_id', 'days', 'evaluated_on', 'reason'} \
                    or any(len(form.getlist(k)) != 1 for k in form):
                raise ValueError('Exact fields required.')
            leave_type_id = int(str(form['leave_type_id']))
            payroll_item_id = int(str(form['payroll_item_id']))
            days = parse_cash_days(str(form['days']))
            evaluated = date.fromisoformat(str(form['evaluated_on']))
            reason = str(form['reason']).strip()
            if not reason or len(reason) > 1000:
                raise ValueError('Explicit bounded review required.')
            auth = identity(request, admin)
            actor = admin.username

            def transaction():
                with SessionLocal() as db:
                    # Exclusive-first gate before the service takes table locks.
                    bounded_workbook_operation(_guard)(db, **auth)
                    result = review_cash_out(db, employee_id=employee_id, leave_type_id=leave_type_id,
                        days=days, payroll_item_id=payroll_item_id, evaluated_on=evaluated, **auth)
                    proof = signer.dumps({'employee_id': employee_id, 'leave_type_id': leave_type_id,
                        'payroll_item_id': payroll_item_id, 'days': canonical_amount(days),
                        'evaluated_on': evaluated.isoformat(), 'reason': reason,
                        'digest': result['reviewed_digest'], 'operation_id': secrets.token_hex(32),
                        'identity': auth})
                    employee, types, items = choices(db, employee_id)
                    return render(request, actor, employee=employee, leave_types=types, payroll_items=items,
                                  review=result, proof=proof, reason=reason,
                                  evaluated_on=evaluated.isoformat(), days=str(form['days']),
                                  selected_type=leave_type_id, selected_item=payroll_item_id)
            return await run_in_threadpool(transaction)
        except (ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError) as exc:
            fail(exc)

    @router.post(root + '/confirm', dependencies=[Depends(csrf_protect)])
    async def confirm(request: Request, employee_id: int, admin: AdminAccount = Depends(require_system_admin)):
        if request.scope.get('path') != f'/leave/cash-out/{employee_id}/confirm':
            raise HTTPException(400, 'Canonical operation path required.')
        try:
            form = await request.form()
            if set(form) != {'csrf_token', 'proof', 'human_confirmed'} \
                    or any(len(form.getlist(k)) != 1 for k in form) \
                    or form['human_confirmed'] != 'yes' or len(str(form['proof'])) > 12000:
                raise ValueError('Explicit confirmation required.')
            proof = signer.loads(str(form['proof']), max_age=1800)
            auth = identity(request, admin)
            actor = admin.username
            if (type(proof) is not dict or set(proof) != {'employee_id', 'leave_type_id', 'payroll_item_id',
                    'days', 'evaluated_on', 'reason', 'digest', 'operation_id', 'identity'}
                    or proof['identity'] != auth or proof['employee_id'] != employee_id):
                raise ValueError('Review identity changed.')

            def transaction():
                with SessionLocal() as db:
                    # Exclusive-first gate before the service takes table locks.
                    bounded_workbook_operation(_guard)(db, **auth)
                    result = apply_cash_out(db, operation_id=proof['operation_id'],
                        reviewed_digest=proof['digest'], human_confirmed=True, reason=proof['reason'],
                        employee_id=employee_id, leave_type_id=proof['leave_type_id'],
                        days=Fraction(proof['days']), payroll_item_id=proof['payroll_item_id'],
                        evaluated_on=date.fromisoformat(proof['evaluated_on']), **auth)
                    employee, types, items = choices(db, employee_id)
                    response = render(request, actor, employee=employee, leave_types=types,
                                      payroll_items=items, saved=result)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature, ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError) as exc:
            fail(exc)

    return router
