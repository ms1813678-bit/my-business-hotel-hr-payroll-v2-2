"""Item 4a: tests of CURRENT behavior of the 'separate confirmation' for payroll approval. No behavior is changed or
chosen here. Owner's words (WhatsApp, 2026-11-23 19:32, memory: hotel accounts and rules): for the AI assistant,
payroll approval = review the details first, then a separate confirmation step; attendance-completion confirmation
by the HR user's own hand. What code defines today (app/ai_gateway.py, app/main.py):
  prepare (pending row, 10 min TTL, snapshot) -> /ai/review (returns an HMAC receipt) -> /ai/approve (needs the receipt;
  re-checks the snapshot) and only approve_attendance_month also needs the checkbox. The direct payroll page approve
  route is one CSRF POST, no review step. Undefined parts are listed in the notes passed with this file."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.ai_gateway as gateway
from app import main
from app.csrf import csrf_protect
from app.dependencies import require_payroll_access, require_system_admin
from tests.test_ai_gateway import _memory_engine

PLAN = {'action': 'approve_payroll_run', 'payroll_run_id': 3}


def pending_row(token, actor='tester', plan=PLAN, minutes=10):
    from app.models import AICommandApproval
    now = datetime.now(timezone.utc)
    with gateway.SessionLocal.begin() as db:
        db.add(AICommandApproval(token_hash=gateway._token_hash(token), actor=actor, question='Approve payroll run 3.',
            plan_json=gateway._canonical_json(plan), plan_hash=gateway.plan_hash(plan), status='pending',
            created_at=now, expires_at=now + timedelta(minutes=minutes)))


def test_receipt_binds_token_actor_and_plan_hash_and_is_stateless(monkeypatch):
    engine = _memory_engine(monkeypatch)
    try:
        h = gateway.plan_hash(PLAN)
        base = gateway.review_receipt('t1', 'tester', h)
        assert len(base) == 64 and base == gateway.review_receipt('t1', 'tester', h)
        assert len({base, gateway.review_receipt('t2', 'tester', h), gateway.review_receipt('t1', 'other', h),
                    gateway.review_receipt('t1', 'tester', gateway.plan_hash({**PLAN, 'payroll_run_id': 4}))}) == 4
        pending_row('t1')
        # Current behavior: nothing records that the review screen was shown. The receipt is only an HMAC that the
        # review step returns, so possession of it for this token, actor and plan is what 'reviewed' means.
        assert gateway.verify_review_receipt('t1', 'tester', base) is True
    finally:
        engine.dispose()


def test_verify_receipt_refuses_other_actor_unknown_token_and_malformed(monkeypatch):
    engine = _memory_engine(monkeypatch)
    try:
        pending_row('t1')
        good = gateway.review_receipt('t1', 'tester', gateway.plan_hash(PLAN))
        assert gateway.verify_review_receipt('t1', 'someone-else', good) is False
        assert gateway.verify_review_receipt('unknown', 'tester', good) is False
        for bad in ('', good.upper(), good[:-1], good + '0', 'é' * 64, None, 5):
            assert gateway.verify_review_receipt('t1', 'tester', bad) is False
    finally:
        engine.dispose()


def test_expired_request_is_not_executed_and_ttl_is_ten_minutes(monkeypatch):
    engine = _memory_engine(monkeypatch)
    try:
        assert gateway.APPROVAL_TTL_MINUTES == 10
        pending_row('old', minutes=-1)
        with pytest.raises(ValueError):
            gateway.approve_command('old', 'tester', language='en')
        from sqlalchemy import select
        from app.models import AICommandApproval
        with gateway.SessionLocal() as db:
            row = db.scalar(select(AICommandApproval).where(AICommandApproval.token_hash == gateway._token_hash('old')))
            assert row.status == 'expired'
    finally:
        engine.dispose()


@pytest.fixture
def client(monkeypatch):
    admin = SimpleNamespace(username='synthetic', display_name='Synthetic', role='system_admin')
    main.app.dependency_overrides[require_system_admin] = lambda: admin
    main.app.dependency_overrides[require_payroll_access] = lambda: admin
    main.app.dependency_overrides[csrf_protect] = lambda: None
    monkeypatch.setattr(main, 'recent_memory', lambda *a: [])
    monkeypatch.setattr(main, 'local_model_status', lambda *a: {'available': False, 'message': '', 'model': 'Local'})
    monkeypatch.setattr(main, 'build_smart_scan', lambda *a: [])
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def test_ai_approve_route_has_no_payroll_specific_second_attestation(client, monkeypatch):
    """The route only checks the receipt. The human checkbox is passed through as False for a payroll plan."""
    seen = {}
    monkeypatch.setattr(main, 'verify_review_receipt', lambda *a: True)
    monkeypatch.setattr(main, 'approve_command', lambda *a, **k: seen.update(k) or 'done')
    client.post('/ai/approve', data={'token': 'x', 'review_receipt': 'r'})
    assert seen['human_coverage_confirmed'] is False


def test_direct_payroll_approve_route_is_a_single_csrf_post_with_no_review_step(client, monkeypatch):
    """Current behavior to decide on: the payroll page approves a run on one POST (system admin or finance role)."""
    calls = []
    run = SimpleNamespace(id=7)

    class FakeDb:
        def scalar(self, *_):
            return run
    class FakeCtx:
        def __enter__(self): return FakeDb()
        def __exit__(self, *a): return False
    monkeypatch.setattr(main.SessionLocal, 'begin', lambda: FakeCtx())
    monkeypatch.setattr('app.overtime_day_award.monthly_lock_scope', lambda db: None)
    monkeypatch.setattr(main, 'approve_payroll_run', lambda db, r, actor: calls.append((r.id, actor)))
    response = client.post('/payroll/runs/7/approve', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/payroll/runs/7'
    assert calls == [(7, 'synthetic')]
