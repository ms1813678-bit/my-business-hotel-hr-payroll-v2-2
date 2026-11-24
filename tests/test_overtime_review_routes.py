from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import main, overtime_review_routes
from app.dependencies import require_payroll_access
from app.models import AttendanceDailyRecord
from tests.test_leave_exact_adjustment import adjustment_store  # noqa: F401
from tests.test_leave_precision_migration import precision_schema  # noqa: F401


@pytest.fixture
def ot_client(adjustment_store, monkeypatch):  # noqa: F811
    engine, proposal, _kwargs = adjustment_store
    with Session(engine) as db, db.begin():
        db.add(AttendanceDailyRecord(employee_id=proposal.employee_id,
            work_date=date(2026, 1, 2), worked_minutes=600, status='needs_review'))
    monkeypatch.setattr(overtime_review_routes, 'SessionLocal', lambda: Session(engine))
    main.app.dependency_overrides[require_payroll_access] = lambda: SimpleNamespace(
        username='synthetic', display_name='Synthetic', role='system_admin')
    try:
        with TestClient(main.app) as client:
            yield client, proposal.employee_id
    finally:
        main.app.dependency_overrides.clear()


def test_ot_list_detail_unknowns_and_disabled_confirmation(ot_client):
    client, employee_id = ot_client
    listing = client.get('/overtime/review', params={'employee_id': employee_id})
    assert listing.status_code == 200
    assert '600' in listing.text and '2026-01-02' in listing.text
    detail = client.get('/overtime/review', params={
        'employee_id': employee_id, 'work_date': '2026-01-02'})
    assert 'id="overtime-day-detail"' in detail.text
    assert 'disabled' in detail.text
    assert 'action="/overtime/approve"' not in detail.text
    assert client.post('/overtime/review').status_code == 405
    missing = client.get('/overtime/review', params={
        'employee_id': employee_id, 'work_date': '2026-01-03'})
    assert 'سجل حضور هذا اليوم غير متاح.' in missing.text


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_new_ot_content_is_localized(ot_client, language):
    import re

    client, employee_id = ot_client
    client.cookies.set('my_hr_language', language)
    response = client.get('/overtime/review', params={
        'employee_id': employee_id, 'work_date': '2026-01-02'})
    content = response.text.split('<main class="content">', 1)[1].split('<footer>', 1)[0]
    assert re.search(r'[\u0600-\u06ff]', content) is None, re.findall(r'[^<>]*[\u0600-\u06ff][^<>]*', content)


def test_ot_route_requires_authentication():
    with TestClient(main.app) as client:
        assert client.get('/overtime/review', follow_redirects=False).status_code in (303, 401, 403)
