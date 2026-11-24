from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import leave_workbench_routes, main
from app.dependencies import require_leave_read
from app.models import LeaveRegisterEntry
from tests.test_leave_exact_adjustment import adjustment_store  # noqa: F401
from tests.test_leave_precision_migration import precision_schema  # noqa: F401


@pytest.fixture
def workbench_client(adjustment_store, monkeypatch):  # noqa: F811
    engine, proposal, _kwargs = adjustment_store
    with Session(engine) as db, db.begin():
        db.add(LeaveRegisterEntry(employee_id=proposal.employee_id, work_date=date(2026, 1, 2),
            code=None, source='synthetic', recorded_by='synthetic'))
    monkeypatch.setattr(leave_workbench_routes, 'SessionLocal', lambda: Session(engine))
    identity = SimpleNamespace(username='synthetic', display_name='Synthetic', role='system_admin')
    main.app.dependency_overrides[require_leave_read] = lambda: identity
    try:
        with TestClient(main.app) as client:
            yield client, proposal.employee_id, identity
    finally:
        main.app.dependency_overrides.clear()


def test_employee_first_explicit_period_and_roles(workbench_client):
    client, employee_id, identity = workbench_client
    query = {'employee_id': employee_id, 'period_start': '2026-01-01', 'period_end': '2026-01-31'}
    page = client.get('/leave/workbench', params=query)
    assert page.status_code == 200
    assert 'غير معروف' in page.text
    assert 'الأرصدة المسجلة بالأيام' in page.text
    identity.role = 'assistant_manager'
    page = client.get('/leave/workbench', params=query)
    assert 'بيانات الأيام المسجلة' in page.text
    assert 'الأرصدة المسجلة بالأيام' not in page.text
    identity.role = 'finance'
    page = client.get('/leave/workbench', params=query)
    assert 'الأرصدة المسجلة بالأيام' in page.text
    assert 'بيانات الأيام المسجلة' not in page.text
    assert 'method="post" action="/leave/workbench' not in page.text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_workbench_ui_labels_localized(workbench_client, language):
    import re

    client, employee_id, _identity = workbench_client
    client.cookies.set('my_hr_language', language)
    page = client.get('/leave/workbench', params={'employee_id': employee_id,
        'period_start': '2026-01-01', 'period_end': '2026-01-31'})
    content = page.text.split('<main class="content">')[1].split('<footer>')[0]
    assert re.search(r'[\u0600-\u06ff]', content) is None, re.findall(r'[^<>]*[\u0600-\u06ff][^<>]*', content)


def test_corrected_scope_has_no_manual_request_or_day_write_endpoint(workbench_client):
    client, employee_id, _identity = workbench_client
    for path in ('/leave/workbench/review', '/leave/workbench/confirm'):
        assert client.post(path, data={}).status_code == 404
    page = client.get('/leave/workbench', params={'employee_id': employee_id})
    assert 'action="/leave/workbench/review"' not in page.text
    assert 'لا توجد دورة تقديم طلب إجازة جديدة.' in page.text
