"""Negative and concurrency HTTP checks for the annual settlement routes (fresh migrated DB only)."""
import json
import re
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main
from app.db import SessionLocal
from app.models import AdminAccount, LeaveAnnualSettlement, LeaveCashOut, LeaveMonthEarning, PayrollItem
from app.security import hash_password
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_cash_out import payroll
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_shift_day_http import token

PASSWORD = 'Synthetic-Annual-Only-1500!'


def login(client, username):
    r = client.post('/login', data=dict(csrf_token=token(client.get('/login')), username=username,
                    password=PASSWORD), follow_redirects=False)
    assert r.status_code == 303, r.status_code


def counts():
    with SessionLocal() as db:
        return (db.scalar(select(func.count()).select_from(LeaveAnnualSettlement)),
                db.scalar(select(func.count()).select_from(LeaveCashOut)))


@pytest.mark.parametrize('prior_year', [{'hire_date': date(2025, 1, 1), 'used_annual': 6}], indirect=True)
def test_annual_http_negative_and_race(prior_year, monkeypatch):
    db, employee, kinds, args = prior_year
    actor = db.get(AdminAccount, args['actor_id'])
    actor.password_hash = hash_password(PASSWORD)
    for role in ('assistant_manager', 'finance'):
        db.add(AdminAccount(username='synthetic-annual-' + role, password_hash=hash_password(PASSWORD),
                            role=role, active=True))
    run, item = payroll(db, employee, start=date(2025, 12, 1), end=date(2025, 12, 31), status='draft')
    item_id = item.id; username = actor.username; db.commit()
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    root = '/leave/annual-settlement/' + args['selection_id']
    other = '/leave/annual-settlement/' + 'b' * 64
    form = dict(reviewed_on='2026-01-01', payroll_item_id=str(item_id), available_from='2026-01-01',
                available_through='2026-04-30', reason='Synthetic negative review')

    def review(client):
        page = client.get(root)
        assert page.status_code == 200
        r = client.post(root + '/review', data=dict(csrf_token=token(page), **form))
        return r, token(r) if r.status_code == 200 else None

    # (a) other roles are refused on every route
    for role in ('assistant_manager', 'finance'):
        with TestClient(main.app) as c:
            login(c, 'synthetic-annual-' + role)
            csrf = token(c.get('/login')) if False else None
            assert c.get(root).status_code == 403
            home = c.get('/leave/workbench')
            t = token(home) if 'csrf_token' in home.text else 'x'
            assert c.post(root + '/review', data=dict(csrf_token=t, **form)).status_code in (403,)
            assert c.post(root + '/confirm', data=dict(csrf_token=t, proof='x', human_confirmed='yes')).status_code in (403,)
    assert counts() == (0, 0)

    with TestClient(main.app) as client, TestClient(main.app) as second:
        login(client, username); login(second, username)
        reviewed, csrf = review(client)
        assert reviewed.status_code == 200
        proof = re.search('name="proof" value="([^"]+)"', reviewed.text).group(1)
        good = dict(csrf_token=csrf, proof=proof, human_confirmed='yes')

        # (b) proof posted to a different selection
        assert client.post(other + '/confirm', data=good).status_code == 400
        # (c) proof from another session of the same account
        assert second.post(root + '/confirm', data=dict(good, csrf_token=token(second.get(root)))).status_code == 400
        # (d) expired proof
        import time as _time
        real = _time.time
        monkeypatch.setattr('itsdangerous.timed.time.time', lambda: real() + 4000)
        assert client.post(root + '/confirm', data=good).status_code == 400
        monkeypatch.undo()
        monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
        monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
        assert counts() == (0, 0)

        # Epoch changes invalidate an already signed proof, without posting.
        from app.models import SystemSetting
        from app.restore_lock import RESTORE_EPOCH_KEY
        with SessionLocal.begin() as s:
            setting = s.get(SystemSetting, RESTORE_EPOCH_KEY)
            previous_epoch = setting.value if setting else None
            s.merge(SystemSetting(key=RESTORE_EPOCH_KEY,value='synthetic-annual-new-epoch',updated_by=username))
        try:
            assert client.post(root + '/confirm',data=good).status_code in (400,401,403,409)
            assert counts() == (0,0)
        finally:
            with SessionLocal.begin() as s:
                setting = s.get(SystemSetting,RESTORE_EPOCH_KEY)
                if previous_epoch is None: s.delete(setting)
                else: setting.value=previous_epoch

        # (f) stale payroll basis between review and confirm: nothing written
        with SessionLocal.begin() as s:
            s.get(PayrollItem, item_id).performance_bonus = s.get(PayrollItem, item_id).performance_bonus + 1
        assert client.post(root + '/confirm', data=good).status_code == 400
        assert counts() == (0, 0)
        with SessionLocal.begin() as s:
            s.get(PayrollItem, item_id).performance_bonus = s.get(PayrollItem, item_id).performance_bonus - 1

        # (h) corrupt saved source value: controlled 400, nothing written
        with SessionLocal() as s:
            month = s.scalar(select(LeaveMonthEarning).where(LeaveMonthEarning.selection_id == args['selection_id'],
                             LeaveMonthEarning.month_start == date(2025, 12, 1)))
            original = month.source_json
        saved = json.loads(original)
        saved['current_insurance_years'][0][2] = 'abc'
        # immutable receipt triggers may block this; bypass only inside this scratch database
        with SessionLocal.begin() as s:
            s.execute(__import__('sqlalchemy').text('ALTER TABLE leave_month_earnings DISABLE TRIGGER USER'))
            s.execute(__import__('sqlalchemy').text('UPDATE leave_month_earnings SET source_json=:v WHERE selection_id=:s AND month_start=DATE \'2025-12-01\''),
                      dict(v=json.dumps(saved), s=args['selection_id']))
        try:
            assert client.get(root).status_code == 400
            page = client.get(root)
        finally:
            with SessionLocal.begin() as s:
                s.execute(__import__('sqlalchemy').text('UPDATE leave_month_earnings SET source_json=:v WHERE selection_id=:s AND month_start=DATE \'2025-12-01\''),
                          dict(v=original, s=args['selection_id']))
                s.execute(__import__('sqlalchemy').text('ALTER TABLE leave_month_earnings ENABLE TRIGGER USER'))
        assert counts() == (0, 0)

        # two-session confirm race: two different operations on the same source, exactly one wins
        r1, c1 = review(client)
        r2, c2 = review(second)
        p1 = re.search('name="proof" value="([^"]+)"', r1.text).group(1)
        p2 = re.search('name="proof" value="([^"]+)"', r2.text).group(1)
        out = {}
        gate = threading.Barrier(2)

        def go(name, cl, c, p):
            gate.wait()
            out[name] = cl.post(root + '/confirm', data=dict(csrf_token=c, proof=p, human_confirmed='yes')).status_code
        ts = [threading.Thread(target=go, args=('a', client, c1, p1)),
              threading.Thread(target=go, args=('b', second, c2, p2))]
        [t.start() for t in ts]; [t.join(40) for t in ts]
        # exactly one winner; the loser is refused (400 stale, or 503 busy-retry when it hit the lock timeout)
        assert sorted(out.values())[0] == 200 and sorted(out.values())[1] in (400, 503), out
        assert counts() == (1, 1)
        loser = (second, c2, p2) if out['a'] == 200 else (client, c1, p1)
        retry = loser[0].post(root + '/confirm', data=dict(csrf_token=loser[1], proof=loser[2], human_confirmed='yes'))
        assert retry.status_code == 400, retry.status_code
        assert counts() == (1, 1)
