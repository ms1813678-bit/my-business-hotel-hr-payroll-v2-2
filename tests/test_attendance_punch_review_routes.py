"""Punch-review screen over a synthetic Att.log fixture: real middleware, auth and CSRF, nothing written."""
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func, delete, text

from app import main
from app.attendance_punch_review_routes import punch_review_router, build_review_view
from app.db import SessionLocal
from app.models import AdminAccount, AdminSession, AttendanceDailyRecord, AttendanceImportBatch, Department, Employee
from app.security import hash_password
from tests.test_attendance_punch_review import fixture_bytes

PASSWORD = 'Synthetic-Only-Login-1004!'
if not any(getattr(r, 'path', None) == '/attendance/punch-review' for r in main.app.routes):
    main.app.include_router(punch_review_router(main.templates))   # the writer registers this in app/main.py


def token(response):
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


@pytest.fixture()
def seeded(monkeypatch):
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    with SessionLocal.begin() as db:
        department = Department(name='Synthetic punch review', active=True); db.add(department); db.flush()
        for fp, code, name in (('7', 'SYN-PR-1', 'Synthetic One'), ('8', 'SYN-PR-2', 'Synthetic Two')):
            db.add(Employee(code=code, full_name=name, fingerprint_id=fp, department_id=department.id, position='Synthetic',
                            hire_date=date(2025, 1, 1), status='active', basic_salary=Decimal(1), daily_hours=Decimal(8)))
        for role in ('system_admin', 'finance', 'assistant_manager'):
            account = db.scalar(select(AdminAccount).where(AdminAccount.username == 'syn-pr-' + role))
            if account is None:        # accounts are never deleted (a database guard protects them); they stay, inactive when idle
                account = AdminAccount(username='syn-pr-' + role, password_hash=hash_password(PASSWORD), role=role, must_change_password=False)
                db.add(account)
            account.active = True
        db.flush()
        department_id = department.id
    yield department_id
    with SessionLocal.begin() as db:
        db.execute(delete(Employee).where(Employee.code.in_(['SYN-PR-1', 'SYN-PR-2'])))
        db.execute(delete(Department).where(Department.id == department_id))
        # CI sessions share one database and later tests need admin_accounts and admin_sessions empty, so remove
        # what this file created (same bypass of the sole-admin delete guard that tests/test_g5t_restore_security_real_pg.py uses).
        db.execute(text("SET LOCAL session_replication_role = replica"))
        ids = [i for i, in db.execute(select(AdminAccount.id).where(AdminAccount.username.like('syn-pr-%')))]
        if ids:
            db.execute(delete(AdminSession).where(AdminSession.admin_account_id.in_(ids)))
            db.execute(delete(AdminAccount).where(AdminAccount.id.in_(ids)))


def counts():
    with SessionLocal() as db:
        return (db.scalar(select(func.count(AttendanceImportBatch.id))), db.scalar(select(func.count(AttendanceDailyRecord.id))))


def login(client, role):
    assert client.post('/login', data=dict(csrf_token=token(client.get('/login')), username='syn-pr-' + role, password=PASSWORD), follow_redirects=False).status_code == 303


def test_view_shows_only_review_days_and_drops_nothing(seeded):
    with SessionLocal() as db:
        view = build_review_view(fixture_bytes(), 'att.xlsx', db)
    assert view['dropped'] == 0 and view['total_days'] > view['review_days'] == 4
    assert {(c['work_date'], c['is_duplicate'], c['is_unreadable']) for c in view['cards']} == {
        ('2026-07-04', False, True), ('2026-07-08', True, False), ('2026-07-09', False, True), ('2026-07-10', False, True)}
    dup = next(c for c in view['cards'] if c['is_duplicate'])
    assert dup['duplicates'] == [{'time': '15:30:00', 'count': 2}] and dup['employee_code'] == 'SYN-PR-1'
    assert dup['punch_times'].count('15:30:00') == 2


@pytest.mark.parametrize('name,raw,reason', [('a.csv', b'x', 'unsupported_extension'), ('a.xlsx', b'', 'empty_file'),
                                             ('a.xlsx', b'not a workbook', 'unreadable_file'),
                                             ('a.xlsx', b'0' * (5 * 1024 * 1024 + 1), 'file_too_large')])
def test_view_refuses_bad_input(seeded, name, raw, reason):
    with SessionLocal() as db, pytest.raises(ValueError, match=reason):
        build_review_view(raw, name, db)


def test_http_auth_csrf_roles_and_nothing_written(seeded):
    before = counts()
    with TestClient(main.app) as client:
        assert client.get('/attendance/punch-review').status_code == 401
        assert client.post('/attendance/punch-review', files={'file': ('a.xlsx', fixture_bytes())}).status_code in (401, 403)
        with TestClient(main.app) as c:        # finance is read-only on attendance: the upload page is closed to it
            login(c, 'finance')
            assert c.get('/attendance/punch-review').status_code == 403
            assert '/attendance/punch-review' not in c.get('/attendance/import').text      # no dead link for finance
            fin_page = c.get('/attendance/import')
            fin_token = token(fin_page)
            assert c.post('/attendance/punch-review', data={'csrf_token': fin_token}, files={'file': ('a.xlsx', fixture_bytes())}).status_code == 403
        for role in ('system_admin', 'assistant_manager'):
            with TestClient(main.app) as c:
                login(c, role)
                page = c.get('/attendance/punch-review')
                assert page.status_code == 200 and 'مراجعة البصمات الخام' in page.text
                assert page.headers['cache-control'] == 'no-store'
                assert '/attendance/punch-review' in c.get('/attendance/import').text
        login(client, 'system_admin')
        page = client.get('/attendance/punch-review')
        files = {'file': ('att.xlsx', fixture_bytes())}
        assert client.post('/attendance/punch-review', files=files).status_code == 403      # no CSRF token
        ok = client.post('/attendance/punch-review', data={'csrf_token': token(page)}, files={'file': ('att.xlsx', fixture_bytes())})
        assert ok.status_code == 200 and '10:1715:2915:3015:30' in ok.text and 'مكررة' in ok.text
        assert 'غير مقروءة' in ok.text and 'SYN-PR-1' in ok.text and 'بصمات محذوفة' not in ok.text
        bad = client.post('/attendance/punch-review', data={'csrf_token': token(page)}, files={'file': ('a.csv', b'x')})
        assert bad.status_code == 400 and 'صيغة الملف غير مدعومة' in bad.text
        missing = client.post('/attendance/punch-review', data={'csrf_token': token(page)})
        assert missing.status_code == 400 and 'الملف فارغ' in missing.text      # a page, not framework JSON
        assert 'href="/attendance/punch-review"' in client.get('/attendance/import').text   # entry link
    assert counts() == before
