"""Cash-out review on an approved or closed payroll item is refused over real HTTP (400); a draft item is accepted.
Run by test_leave_cash_out_approved_item_http_pg.py against a scratch migrated database. Synthetic data only."""
from fastapi.testclient import TestClient

from app import main
from app.db import SessionLocal
from app.models import PayrollItem, PayrollRun
from tests.cash_out_http_acceptance import csrf, login, seed


def _set_status(item_id, status):
    with SessionLocal() as db:
        item = db.get(PayrollItem, item_id)
        run = db.get(PayrollRun, item.payroll_run_id)
        run.status = status
        db.commit()


def test_review_refused_for_approved_and_closed_item_but_accepted_for_draft(monkeypatch):
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    employee_id, type_id, item_id = seed()
    url = f'/leave/cash-out/{employee_id}'
    with TestClient(main.app) as client:
        login(client, 'synthetic-cashout-hr')

        def review():
            page = client.get(url)
            data = dict(csrf_token=csrf(page), leave_type_id=str(type_id), payroll_item_id=str(item_id),
                        days='2', evaluated_on='2026-04-02', reason='Synthetic explicit cash-out review')
            return client.post(url + '/review', data=data)

        for status in ('approved', 'closed'):
            _set_status(item_id, status)
            r = review()
            assert r.status_code == 400, (status, r.status_code)
            assert 'id="cash-out-review"' not in r.text
        _set_status(item_id, 'draft')
        ok = review()
        assert ok.status_code == 200 and 'id="cash-out-review"' in ok.text
