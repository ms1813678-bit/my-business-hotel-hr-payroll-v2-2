"""Inner acceptance body for the owner-side ZERO decision adversarial review.

Runs inside a fresh migrated database created by tests/test_zero_ui_owner_adversarial.py
(same harness shape as the author's zero_decision_http_acceptance.py). One sequential
test over one completed-2025 world; phases are independent of the author's covered cases.
"""
import re

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select

from app import main
from app.config import settings
from app.db import SessionLocal
from app.models import AdminAccount, AuditLog, Employee, LeaveBalanceTransaction, LeaveZeroDecision
from app.security import hash_password

from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_shift_day_http import token

PASSWORD = 'Synthetic-Zero-Adversarial-2026!'


def _counts():
    with SessionLocal() as db:
        return (db.scalar(select(func.count()).select_from(LeaveZeroDecision)),
                db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)),
                db.scalar(select(func.count()).select_from(AuditLog)))


def _proof(client, root, data):
    reviewed = client.post(root + '/review', data=data)
    assert reviewed.status_code == 200, reviewed.text[:400]
    return reviewed, re.search('name="proof" value="([^"]+)"', reviewed.text).group(1)


@pytest.mark.parametrize('prior_year', [{}], indirect=True)
def test_zero_owner_adversarial_phases(prior_year, monkeypatch):
    db, employee, kinds, args = prior_year
    actor = db.get(AdminAccount, args['actor_id'])
    actor.password_hash = hash_password(PASSWORD)
    username = actor.username
    db.commit()
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    employee_id = employee.id
    reviewed_on = args['reviewed_on'].isoformat()
    root = f'/leave/zero-decision/{employee_id}'
    data = dict(choice=f'{kinds["W"]}:2025', reviewed_on=reviewed_on,
                reason='Synthetic adversarial zero')

    with TestClient(main.app) as client:
        login = client.get('/login')
        assert client.post('/login', data=dict(csrf_token=token(login), username=username,
                                               password=PASSWORD),
                           follow_redirects=False).status_code == 303

        # phase 1: forged / cross-salt / oversized / duplicate inputs all refuse, nothing records
        page = client.get(root)
        assert page.status_code == 200, page.text[:400]
        form = {**data, 'csrf_token': token(page)}
        dup = [(k, v) for k, v in form.items()] + [('choice', f'{kinds["H"]}:2025')]
        assert client.post(root + '/review', data=dup).status_code in (400, 403)
        assert client.post(root + '/review', data={**form, 'reason': 'x' * 1001}).status_code == 400
        reviewed, proof = _proof(client, root, form)
        confirm = dict(csrf_token=token(reviewed), proof=proof, human_confirmed='yes')
        assert client.post(root + '/confirm',
                           data={**confirm, 'proof': 'x' * 12001}).status_code == 400
        mine = URLSafeTimedSerializer(settings.session_secret, salt='zero-decision-v1')
        body = mine.loads(proof)
        alien = URLSafeTimedSerializer(settings.session_secret, salt='leave-cash-out-v1')
        assert client.post(root + '/confirm',
                           data={**confirm, 'proof': alien.dumps(body)}).status_code == 400
        forged = mine.dumps(dict(employee_id=employee_id, values=dict(data),
                                 digest='0' * 64, operation_id='1' * 64,
                                 identity={'actor_id': 1, 'session_id': 'x',
                                           'session_version': 0, 'expected_epoch': None}))
        assert client.post(root + '/confirm', data={**confirm, 'proof': forged}).status_code == 400
        assert _counts()[0] == 0

        # phase 2: GET + review post nothing; reflected markup is escaped
        with SessionLocal() as change:
            change.get(Employee, employee_id).full_name = '<script>alert(1)</script>'
            change.commit()
        before = _counts()
        page = client.get(root)
        assert page.status_code == 200, page.text[:400]
        assert '<script>alert(1)</script>' not in page.text
        reviewed = client.post(root + '/review',
                               data={**form, 'csrf_token': token(page),
                                     'reason': '<img src=x onerror=alert(2)>'})
        assert reviewed.status_code == 200, reviewed.text[:400]
        assert '<img src=x onerror=alert(2)>' not in reviewed.text
        assert _counts() == before

        # phase 3: two distinct proofs for one source; second confirm refuses, one debit total
        page = client.get(root)
        form = {**data, 'csrf_token': token(page)}
        reviewed1, proof1 = _proof(client, root, form)
        reviewed2, proof2 = _proof(client, root, form)
        assert proof1 != proof2
        before = _counts()
        saved = client.post(root + '/confirm', data=dict(csrf_token=token(reviewed1),
                                                         proof=proof1, human_confirmed='yes'))
        assert saved.status_code == 200, saved.text[:400]
        second = client.post(root + '/confirm', data=dict(csrf_token=token(reviewed2),
                                                          proof=proof2, human_confirmed='yes'))
        assert second.status_code == 400
        z, ledger, _a = _counts()
        assert z == before[0] + 1 and ledger == before[1] + 1

        # phase 4: a second kind with no positive leftover refuses cleanly through HTTP,
        # no writes, and the confirmed W receipt from phase 3 is untouched
        page = client.get(root)
        hform = dict(csrf_token=token(page), choice=f'{kinds["H"]}:2025',
                     reviewed_on=reviewed_on, reason='Synthetic holiday zero')
        before = _counts()
        refused = client.post(root + '/review', data=hform)
        assert refused.status_code == 400
        assert _counts() == before
        with SessionLocal() as check:
            rows = check.scalars(select(LeaveZeroDecision)).all()
            assert len(rows) == 1 and rows[0].leave_type_id == kinds['W'] and rows[0].source_year == 2025
