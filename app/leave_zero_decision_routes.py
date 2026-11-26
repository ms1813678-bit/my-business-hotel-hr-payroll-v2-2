# ruff: noqa: B008
"""HR-only explicit zero decision for completed-year weekly-rest/holiday leftovers."""
from datetime import date
from decimal import Decimal
from fractions import Fraction
import json
import re
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from app.config import settings
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal
from app.dependencies import require_system_admin
from app.department_workbook_store import _guard
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_exact_amount import display_days
from app.leave_policy_store import policy_read_guard
from app.leave_zero_decision import review_zero_decision, record_zero_decision
from app.models import AdminAccount, Employee, LeaveType, LeaveMonthEarning


def zero_decision_router(templates):
    router = APIRouter()
    signer = URLSafeTimedSerializer(settings.session_secret, salt='zero-decision-v1')
    fields = {'csrf_token', 'choice', 'reviewed_on', 'reason'}

    def identity(request, admin):
        return dict(actor_id=admin.id, session_id=request.session.get('admin_session_id'),
            session_version=request.session.get('admin_session_version'), expected_epoch=request.state.restore_epoch)

    def options(db, employee_id, auth, *, writing):
        (bounded_workbook_operation(_guard) if writing else policy_read_guard)(db, **auth)
        employee = db.get(Employee, employee_id, populate_existing=True)
        if employee is None:
            raise ValueError('Employee required.')
        from app.leave_opening_source import _current
        opening=_current(db,employee_id,auth['expected_epoch'])
        if opening is None:
            raise ValueError('Confirmed source selection required.')
        kinds = {k.id: k for k in db.scalars(select(LeaveType).where(LeaveType.code.in_(['weekly_rest', 'official_holiday']))).all()}
        from app.leave_opening_rebind import lineage_ids
        allowed=lineage_ids(db,opening.id)
        months = db.scalars(select(LeaveMonthEarning).where(LeaveMonthEarning.employee_id == employee_id,
            LeaveMonthEarning.selection_id.in_(allowed)).order_by(LeaveMonthEarning.month_start).limit(600)).all()
        years = {m.month_start.year: m for m in months if m.month_start.month == 12}
        choices = [dict(value=f'{k}:{y}', kind=kinds[k], year=y) for y in sorted(years, reverse=True) for k in sorted(kinds)]
        return employee, opening, years, kinds, choices

    def selection(db, employee_id, values, auth, *, writing):
        employee, opening, years, _kinds, choices = options(db, employee_id, auth, writing=writing)
        if not re.fullmatch('[0-9]{1,9}:[0-9]{4}', values['choice']) or values['choice'] not in {c['value'] for c in choices}:
            raise ValueError('Choose an actual completed-year weekly rest or holiday source.')
        type_id, year = (int(x) for x in values['choice'].split(':'))
        saved = json.loads(years[year].source_json)
        selected = dict(employee_id=employee.id, leave_type_id=type_id, source_year=year, selection_id=opening.id,
            reviewed_on=date.fromisoformat(values['reviewed_on']),
            current_insurance_years={(i, date.fromisoformat(m)): Decimal(v) if v is not None else None
                for i, m, v in saved['current_insurance_years']},
            age_evaluation_dates={date.fromisoformat(m): date.fromisoformat(d) for m, d in saved['age_evaluation_dates']}, **auth)
        return employee, choices, selected

    def render(request, actor, employee, choices, **values):
        context = dict(admin=actor, csrf_token=get_csrf_token(request), employee=employee, choices=choices, choice='',
            reviewed_on='', reason='', review=None, saved=None, proof=None, days=lambda v: display_days(Fraction(v)))
        context.update(values)
        response = templates.TemplateResponse(request=request, name='leave_zero_decision.html', context=context)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def fail(exc):
        code = getattr(getattr(exc, 'orig', None), 'sqlstate', None)
        raise HTTPException(503 if code == '55P03' else 400,
            'تعذر حفظ قرار التصفير. راجع مصدر السنة والخصومات اللاحقة والجلسة؛ لم يسجل خصم جديد.',
            headers={'Cache-Control': 'no-store', **({'Retry-After': '3'} if code == '55P03' else {})}) from exc

    root = '/leave/zero-decision/{employee_id}'

    @router.get(root)
    def page(request: Request, employee_id: int, admin: AdminAccount = Depends(require_system_admin)):
        try:
            with SessionLocal() as db:
                employee, _o, _y, _k, choices = options(db, employee_id, identity(request, admin), writing=False)
                return render(request, admin.username, employee, choices)
        except (ArithmeticError, ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError) as exc:
            fail(exc)

    @router.post(root + '/review', dependencies=[Depends(csrf_protect)])
    async def review(request: Request, employee_id: int, admin: AdminAccount = Depends(require_system_admin)):
        if request.scope['path'] != f'/leave/zero-decision/{employee_id}/review':
            raise HTTPException(400, 'Canonical operation path required.')
        try:
            form = await request.form()
            if set(form) != fields or any(len(form.getlist(k)) != 1 for k in form):
                raise ValueError('Exact zero review fields required.')
            values = {k: str(form[k]) for k in form if k != 'csrf_token'}
            if any(len(v)>1100 for v in values.values()):raise ValueError('Bounded review fields required.')
            auth = identity(request, admin); actor = admin.username

            def transaction():
                with SessionLocal() as db:
                    employee, choices, selected = selection(db, employee_id, values, auth, writing=True)
                    result = review_zero_decision(db, source_selection=selected, reason=values['reason'])
                    proof = signer.dumps(dict(employee_id=employee_id, values=values, digest=result['reviewed_digest'],
                        operation_id=secrets.token_hex(32), identity=auth))
                    return render(request, actor, employee, choices, review=result, proof=proof, **values)
            return await run_in_threadpool(transaction)
        except (ArithmeticError, ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError) as exc:
            fail(exc)

    @router.post(root + '/confirm', dependencies=[Depends(csrf_protect)])
    async def confirm(request: Request, employee_id: int, admin: AdminAccount = Depends(require_system_admin)):
        if request.scope['path'] != f'/leave/zero-decision/{employee_id}/confirm':
            raise HTTPException(400, 'Canonical operation path required.')
        try:
            form = await request.form()
            if (set(form) != {'csrf_token', 'proof', 'human_confirmed'} or any(len(form.getlist(k)) != 1 for k in form)
                    or form['human_confirmed'] != 'yes' or len(str(form['proof'])) > 12000):
                raise ValueError('Explicit zero confirmation required.')
            proof = signer.loads(str(form['proof']), max_age=1800)
            auth = identity(request, admin); actor = admin.username
            if (type(proof) is not dict or set(proof) != {'employee_id', 'values', 'digest', 'operation_id', 'identity'}
                    or proof['identity'] != auth or proof['employee_id'] != employee_id
                    or type(proof['values']) is not dict or set(proof['values']) != fields - {'csrf_token'}):
                raise ValueError('Zero review identity changed.')
            values = proof['values']

            def transaction():
                with SessionLocal() as db:
                    employee, choices, selected = selection(db, employee_id, values, auth, writing=True)
                    result = record_zero_decision(db, operation_id=proof['operation_id'], reviewed_digest=proof['digest'],
                        human_confirmed=True, source_selection=selected, reason=values['reason'])
                    response = render(request, actor, employee, choices, saved=result)
                    db.commit()
                    return response
            return await run_in_threadpool(transaction)
        except (BadSignature, ArithmeticError, ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError) as exc:
            fail(exc)
    return router
