from datetime import date
import os
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app import main
from app.db import SessionLocal
from app.models import AdminAccount, LeaveBalanceTransaction, LeaveZeroDecision, AuditLog
from app.security import hash_password
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_shift_day_http import token
from fractions import Fraction

PASSWORD = 'Synthetic-Zero-Only-2026!'
NAMES = {'ar': 'تصفير رصيد الراحة والعطلة', 'en': 'Zero weekly rest and holiday balance', 'zh': '清零每周休息和假日余额'}


@pytest.mark.parametrize('prior_year', [{}], indirect=True)
def test_actual_zero_review_confirmation_replay_roles(prior_year, monkeypatch):
    language = os.environ.get('HR_ZERO_LANGUAGE', 'ar')
    db, employee, kinds, args = prior_year
    actor = db.get(AdminAccount, args['actor_id']); actor.password_hash = hash_password(PASSWORD)
    username = actor.username; employee_id = employee.id
    employee.full_name = {'ar': 'موظف تجريبي', 'en': 'Test employee', 'zh': '测试员工'}[language]
    wid, hid = kinds['W'], kinds['H']
    reviewed_on = args['reviewed_on'].isoformat()
    before = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)); db.commit()
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    root = f'/leave/zero-decision/{employee_id}'
    reason = {'ar': 'تصفير راحة أسبوعية تجريبي', 'en': 'Synthetic weekly rest zero', 'zh': '测试每周休息清零'}[language]
    with TestClient(main.app) as client:
        client.cookies.set('my_hr_language', language)
        assert client.get(root).status_code in (401, 403)
        assert client.post('/login', data=dict(csrf_token=token(client.get('/login')), username=username, password=PASSWORD),
            follow_redirects=False).status_code == 303
        page = client.get(root); assert page.status_code == 200, page.text[:500]
        assert NAMES[language] in page.text
        assert root in client.get('/leave/workbench', params={'employee_id': employee_id}).text
        data = dict(csrf_token=token(page), choice=f'{wid}:2025', reviewed_on=reviewed_on, reason=reason)
        assert client.post(root + '/review', data={k: v for k, v in data.items() if k != 'csrf_token'}).status_code == 403
        for change in [dict(choice=f'{wid}:2024'), dict(choice='1:2025;x'), dict(choice=f'{kinds["A"]}:2025'),
                       dict(reviewed_on='2025-12-31'), dict(reviewed_on='2999-01-01'), dict(reason=' '), dict(extra='bad')]:
            assert client.post(root + '/review', data={**data, **change}).status_code == 400, change
        assert client.post(f'/leave/zero-decision/{employee_id}/review/', data=data, follow_redirects=False).status_code in (307, 400, 404, 405)
        reviewed = client.post(root + '/review', data=data); assert reviewed.status_code == 200, reviewed.text[:500]
        assert 'id="zero-review"' in reviewed.text and '37/7' in reviewed.text and '-37/7' in reviewed.text
        assert reviewed.headers['cache-control'] == 'no-store'
        proof = re.search('name="proof" value="([^"]+)"', reviewed.text).group(1)
        confirm = dict(csrf_token=token(reviewed), proof=proof, human_confirmed='yes')
        assert client.post(root + '/confirm', data={**confirm, 'proof': proof + 'x'}).status_code == 400
        assert client.post(root + '/confirm', data={k: v for k, v in confirm.items() if k != 'human_confirmed'}).status_code == 400
        assert client.post(root + '/confirm', data={**confirm, 'extra': 'bad'}).status_code == 400
        assert client.post(root + '/confirm', data={**confirm, 'csrf_token': 'x'}).status_code in (400, 403)
        assert client.post(f'/leave/zero-decision/{employee_id + 1}/confirm', data=confirm).status_code == 400
        if os.environ.get('HR_ZERO_SCENARIO') == 'identity':
            # a proof signed in one session cannot be confirmed from another session of the same HR account
            with TestClient(main.app) as other:
                other.cookies.set('my_hr_language', language)
                assert other.post('/login', data=dict(csrf_token=token(other.get('/login')), username=username, password=PASSWORD),
                    follow_redirects=False).status_code == 303
                other_page = other.get(root); assert other_page.status_code == 200
                assert other.post(root + '/confirm', data={**confirm, 'csrf_token': token(other_page)}).status_code == 400
        if os.environ.get('HR_ZERO_SCENARIO') == 'epoch':
            from app.models import SystemSetting
            from app.restore_lock import RESTORE_EPOCH_KEY
            with SessionLocal() as change:
                row = change.scalar(select(SystemSetting).where(SystemSetting.key == RESTORE_EPOCH_KEY))
                if row is None:
                    change.add(SystemSetting(key=RESTORE_EPOCH_KEY, value='synthetic-flipped-epoch', updated_by='synthetic'))
                else:
                    row.value = 'synthetic-flipped-epoch'
                change.commit()
            assert client.post(root + '/confirm', data=confirm).status_code == 400
            assert client.post(root + '/review', data=data).status_code == 400
        if os.environ.get('HR_ZERO_SCENARIO') in ('identity', 'epoch'):
            with SessionLocal() as check:
                assert check.scalar(select(func.count()).select_from(LeaveZeroDecision)) == 0
                assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before
                assert check.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'leave_zero_decided')) == 0
            return
        if os.environ.get('HR_ZERO_SCENARIO')=='stale':
            # a later debit appears after review: reviewed digest cannot be confirmed, and review itself refuses
            with SessionLocal() as change:
                debit = LeaveBalanceTransaction(employee_id=employee_id, leave_type_id=wid, transaction_type='adjustment',
                    days_delta=-1, reason='Synthetic later debit', created_by='synthetic', source_operation_key='month_earning:x:y', source_digest='1' * 64,
                    earning_year=2026, released_on=args['reviewed_on'])
                change.add(debit); change.commit(); debit_id = debit.id
            assert client.post(root + '/confirm', data=confirm).status_code == 400
            assert client.post(root + '/review', data=data).status_code == 400   # negative-debt / later debit never erased
            with SessionLocal() as check:
                assert check.scalar(select(func.count()).select_from(LeaveZeroDecision)) == 0
                assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before + 1
            return
        saved = client.post(root + '/confirm', data=confirm); assert saved.status_code == 200, saved.text[:500]
        assert 'id="zero-saved"' in saved.text
        assert client.post(root + '/confirm', data=confirm).status_code == 200      # exact replay, no second debit
        with SessionLocal() as check:
            assert check.scalar(select(func.count()).select_from(LeaveZeroDecision)) == 1
            assert check.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before + 1
            row = check.scalar(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.source_operation_key.like('leave_zero:%')))
            assert row.leave_type_id == wid and row.earning_year == 2025
            assert check.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'leave_zero_decided')) == 1
        # a second review of the same year is refused (already zeroed / no positive leftover)
        assert client.post(root + '/review', data={**data, 'csrf_token': token(client.get(root))}).status_code == 400
        if os.environ.get('HR_PIXEL_DIR'):
            out = Path(os.environ['HR_PIXEL_DIR']); out.mkdir(parents=True, exist_ok=True)
            for name, response in [('form', page), ('review', reviewed), ('saved', saved)]:
                (out / (name + '.html')).write_text(response.text)
        for role in ('assistant_manager', 'finance'):
            with SessionLocal() as change:
                current = change.scalar(select(AdminAccount).where(AdminAccount.username == username))
                current.role = role; change.commit()
            assert client.get(root).status_code == 403
            assert client.post(root + '/review', data=data).status_code == 403
            assert client.post(root + '/confirm', data=confirm).status_code == 403
            assert root not in client.get('/leave/workbench', params={'employee_id': employee_id}).text
