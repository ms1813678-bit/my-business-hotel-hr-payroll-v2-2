"""Actual normal database HTTP cash-out, authenticated HR, CSRF, flags untouched.

Seeds one custom tracked type through the real audit-chain opening writer
(add_balance_entry) and one explicit payroll item, commits, then drives the
real routes. No auth/guard monkeypatch replaces the real guard: the interleave
test only *holds* the real _guard's return, exactly like the month-earning
acceptance. Synthetic data only.
"""
import os
import re
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main
from app.config import settings
from app.db import SessionLocal
from app.leave_workflow import leave_balance
from app.models import (AdminAccount, AdminSession, Department, Employee,
                        LeaveBalanceTransaction, LeaveCashOut, LeaveType,
                        PayrollItem, PayrollRun)
from app.restore_lock import read_restore_epoch
from app.security import hash_password

PASSWORD = 'Synthetic-CashOut-Only-1500!'


def csrf(page):
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, 'CSRF field missing from page'
    return match.group(1)


def seed():
    with SessionLocal() as db:
        admin = AdminAccount(username='synthetic-cashout-hr', password_hash=hash_password(PASSWORD),
                             role='system_admin', active=True)
        db.add(admin); db.flush()
        session = AdminSession(id='synthetic-cashout-session', admin_account_id=admin.id)
        db.add(session)
        department = Department(name='Synthetic cash-out department', active=True)
        db.add(department); db.flush()
        employee = Employee(code='SYN-CASH', full_name='Synthetic only', department_id=department.id,
                            position='Synthetic', hire_date=date(2025, 1, 1), status='active',
                            basic_salary=Decimal(3000), daily_hours=Decimal(8))
        db.add(employee); db.flush()
        custom = LeaveType(code='synthetic_cash_type', name_ar='مخصص', name_en='Synthetic cash type',
                           tracks_balance=True, active=True)
        db.add(custom); db.flush()
        from app.leave_workflow import add_balance_entry
        add_balance_entry(db, employee_id=employee.id, leave_type=custom,
                          transaction_type='opening', days_delta=Decimal(5),
                          reason='Synthetic cash-out opening', actor=admin.username)
        run = PayrollRun(period_start=date(2026, 3, 1), period_end=date(2026, 3, 31),
                         pay_date=date(2026, 3, 31), status='approved', created_by='synthetic')
        db.add(run); db.flush()
        item = PayrollItem(payroll_run_id=run.id, employee_id=employee.id,
                           employee_code_snapshot=employee.code, employee_name_snapshot=employee.full_name,
                           department_name_snapshot=department.name, position_snapshot=employee.position,
                           salary_basis=Decimal('3000.00'), base_salary=Decimal('3000.00'),
                           daily_hours=Decimal(8), performance_bonus=Decimal('600.00'))
        db.add(item); db.flush()
        for role in ('assistant_manager', 'finance'):
            db.add(AdminAccount(username='synthetic-cashout-' + role,
                                password_hash=hash_password(PASSWORD), role=role, active=True))
        db.commit()
        return employee.id, custom.id, item.id


def login(client, username):
    page = client.get('/login')
    assert client.post('/login', data=dict(csrf_token=csrf(page), username=username,
                       password=PASSWORD), follow_redirects=False).status_code == 303


def test_real_http_cash_out_review_confirm_retry_roles_and_interleave(monkeypatch):
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    employee_id, type_id, item_id = seed()
    assert not main.app.dependency_overrides
    url = f'/leave/cash-out/{employee_id}'
    with TestClient(main.app) as client:
        assert client.get(url).status_code in (401, 403)
        login(client, 'synthetic-cashout-hr')
        page = client.get(url)
        assert page.status_code == 200 and page.headers['cache-control'] == 'no-store'
        data = dict(csrf_token=csrf(page), leave_type_id=str(type_id), payroll_item_id=str(item_id),
                    days='2', evaluated_on='2026-04-02', reason='Synthetic explicit cash-out review')
        assert client.post(url + '/review', data={k: v for k, v in data.items() if k != 'csrf_token'}).status_code == 403
        assert client.post(url + '/review', data=data | {'extra': '1'}).status_code == 400
        reviewed = client.post(url + '/review', data=data)
        assert reviewed.status_code == 200 and 'id="cash-out-review"' in reviewed.text
        assert '240.00' in reviewed.text  # (3000+600)/30 * 2 days, rounded once
        proof = re.search(r'name="proof" value="([^"]+)"', reviewed.text).group(1)
        confirm = dict(csrf_token=csrf(reviewed), proof=proof, human_confirmed='yes')
        assert client.post(url + '/confirm', data=confirm | {'proof': proof + 'x'}).status_code == 400
        # Two-session interleave: hold the first real exclusive guard, the
        # second authenticated confirm must answer busy (503 + Retry-After).
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, Lock
        import app.leave_cash_out_routes as routes
        acquired, finish = Event(), Event()
        once = Lock()
        real_guard = routes._guard

        def held_guard(*args, **kwargs):
            result = real_guard(*args, **kwargs)
            if once.acquire(blocking=False):
                acquired.set()
                assert finish.wait(15), 'first transaction overlap release timeout'
            return result

        def concurrent_confirm(_):
            with TestClient(main.app) as parallel:
                parallel.cookies.update(client.cookies)
                return parallel.post(url + '/confirm', data=confirm)

        with monkeypatch.context() as overlap:
            overlap.setattr(routes, '_guard', held_guard)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(concurrent_confirm, 0)
                try:
                    assert acquired.wait(10), 'first HTTP transaction never held real exclusive guard'
                    second = pool.submit(concurrent_confirm, 1)
                    busy = second.result(timeout=10)
                    assert busy.status_code == 503 and busy.headers.get('Retry-After'), (busy.status_code, busy.text[:250])
                    assert not first.done()
                    with SessionLocal() as check:
                        assert check.scalar(select(func.count()).select_from(LeaveCashOut)) == 0
                finally:
                    finish.set()
                saved = first.result(timeout=10)
        assert saved.status_code == 200 and 'id="cash-out-saved"' in saved.text
        # Exact retry with the same proof: 200, still exactly one receipt/debit.
        assert client.post(url + '/confirm', data=confirm).status_code == 200
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(LeaveCashOut)) == 1
            assert leave_balance(check, employee_id, type_id) == Decimal(3)
    for role in ('assistant_manager', 'finance'):
        with TestClient(main.app) as other:
            login(other, 'synthetic-cashout-' + role)
            assert other.get(url).status_code == 403
            assert other.post(url + '/confirm', data=confirm).status_code in (403, 400)
