from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app import main
from app.dependencies import get_current_admin


@pytest.fixture
def client():
    identity = SimpleNamespace(id=1, role='system_admin', username='synthetic', display_name=None, must_change_password=False)
    def current(request: Request):
        request.state.admin_role = identity.role
        request.state.restore_epoch = None
        return identity
    main.app.dependency_overrides[get_current_admin] = current
    try:
        with TestClient(main.app) as instance:
            yield instance, identity
    finally:
        main.app.dependency_overrides.clear()


def test_manual_form_hr_only_and_post_csrf(client):
    instance, identity = client
    response = instance.get('/leave/opening')
    assert response.status_code == 200
    assert all(f'name="{field}"' in response.text for field in ('annual', 'weekly', 'holiday', 'effective_start'))
    assert instance.post('/leave/opening/review', data={}).status_code == 403
    for role in ('finance', 'assistant_manager'):
        identity.role = role
        assert instance.get('/leave/opening').status_code == 403


@pytest.mark.parametrize('lang', ['en', 'zh'])
def test_manual_form_localized(client, lang):
    import re
    instance, _identity = client
    instance.cookies.set('my_hr_language', lang)
    text = instance.get('/leave/opening').text.split('<main class="content">')[1].split('<footer>')[0]
    assert not re.search(r'[\u0600-\u06ff]', text)


from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def test_real_pg_post_group_review_no_ledger_apply(history_context, monkeypatch):  # noqa: F811
    import re

    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker

    from app.models import LeaveBalanceTransaction, LeaveHistoricalEvidencePreparation
    db, account, auth, fact = history_context
    factory = sessionmaker(bind=db.connection(), autoflush=False, join_transaction_mode='create_savepoint')
    monkeypatch.setattr('app.leave_manual_opening_routes.SessionLocal', factory)
    def current(request: Request):
        request.state.admin_role = account.role
        request.state.restore_epoch = auth['expected_epoch']
        request.session['admin_session_id'] = auth['session_id']
        request.session['admin_session_version'] = auth['session_version']
        return account
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from app.leave_manual_opening_routes import manual_opening_router
    isolated = FastAPI()
    isolated.add_middleware(SessionMiddleware, secret_key='synthetic-only-test-key')
    isolated.include_router(manual_opening_router(main.templates))
    isolated.dependency_overrides[get_current_admin] = current
    before = db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation))
    ledger = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    try:
        with TestClient(isolated) as instance:
            text = instance.get('/leave/opening').text
            token = re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)
            response = instance.post('/leave/opening/review', data={'csrf_token': token,
                'employee_id': fact.employee_id, 'effective_start': '2026-05-17',
                'annual': '8.250000000000000000000001', 'weekly': '2', 'holiday': '0', 'reason': 'Synthetic UI review'})
            assert response.status_code == 200
            assert '8.250000000000000000000001' in response.text
            assert '2026-05-17' in response.text
            assert 'action="/leave/opening/approve"' not in response.text
            assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == before + 3
            assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == ledger
            # Saved group is discoverable without carrying its URL. Stale-epoch
            # history is omitted, and list rendering does not approve anything.
            from app.models import LeaveManualOpeningGroup
            group = db.scalar(select(LeaveManualOpeningGroup))
            history = instance.get('/leave/opening')
            assert history.status_code == 200
            assert '/leave/opening/groups/' + group.id in history.text
            assert history.text.count('<table class="opening-history">') == 1
            assert '<section' not in history.text.split('<title>')[1].split('</title>')[0]
            original_epoch = group.restore_epoch
            group.restore_epoch = 'synthetic-stale-epoch'; db.flush()
            assert '/leave/opening/groups/' + group.id not in instance.get('/leave/opening').text
            group.restore_epoch = original_epoch; db.flush()
            import json
            from hashlib import sha256
            source=db.get(LeaveHistoricalEvidencePreparation,group.annual_id)
            original_json,original_sha=source.proposal_json,source.proposal_sha256
            malformed=json.loads(original_json);del malformed['entries'][0]['days']
            source.proposal_json=json.dumps(malformed);source.proposal_sha256=sha256(source.proposal_json.encode()).hexdigest();db.flush()
            assert instance.get('/leave/opening').status_code==200
            assert '/leave/opening/groups/' + group.id not in instance.get('/leave/opening').text
            assert instance.get('/leave/opening/groups/' + group.id).status_code==404
            source.proposal_json,source.proposal_sha256=original_json,original_sha;db.flush()
            original_digest = group.selection_sha256
            group.selection_sha256 = '0'*64; db.flush()
            assert '/leave/opening/groups/' + group.id not in instance.get('/leave/opening').text
            group.selection_sha256 = original_digest; db.flush()
            # Exempt POST must not open a second DB session after commit or
            # call templates. A concurrent restore can acquire the gate then.
            calls = []
            def one_session_only():
                calls.append(1)
                assert len(calls) == 1, 'Unguarded post-commit DB read'
                return factory()
            monkeypatch.setattr('app.leave_manual_opening_routes.SessionLocal', one_session_only)
            def no_template(*a, **k):
                raise AssertionError('Exempt POST tried rendering after guarded transaction')
            monkeypatch.setattr(main.templates, 'TemplateResponse', no_template)
            replay = instance.post('/leave/opening/review', data={'csrf_token': token,
                'employee_id':fact.employee_id,'effective_start':'2026-05-17',
                'annual':'8.250000000000000000000001','weekly':'2','holiday':'0','reason':'Synthetic UI review'},
                follow_redirects=False)
            assert replay.status_code == 303 and '/leave/opening/groups/' in replay.headers['location']
            assert len(calls)==1

    finally:
        isolated.dependency_overrides.clear()


@pytest.mark.parametrize('failure', ['invalid_input', 'database_error'])
def test_exempt_invalid_post_redirects_without_opening_database(monkeypatch, failure):
    """Invalid values must not read/render using stale POST authorization."""
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware
    from app.leave_manual_opening_routes import manual_opening_router
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key='synthetic-post-no-reads')
    app.include_router(manual_opening_router(main.templates))
    identity = SimpleNamespace(id=1, role='system_admin', username='synthetic', display_name=None)
    app.dependency_overrides[get_current_admin] = lambda: identity
    from app.csrf import get_csrf_token
    @app.get('/synthetic-csrf')
    def get_token(request: Request):
        return {'token':get_csrf_token(request)}
    def no_database():
        if failure == 'database_error':
            from sqlalchemy.exc import SQLAlchemyError
            raise SQLAlchemyError('Synthetic service DB failure')
        raise AssertionError('Exempt invalid POST attempted DB access/render')
    monkeypatch.setattr('app.leave_manual_opening_routes.SessionLocal', no_database)
    with TestClient(app) as instance:
        csrf = instance.get('/synthetic-csrf').json()['token']
        response = instance.post('/leave/opening/review', data={'csrf_token':csrf,
            'employee_id':1,'effective_start':'2026-05-17','annual':'-1' if failure=='invalid_input' else '1',
            'weekly':'2','holiday':'0','reason':'Synthetic'}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/leave/opening?error=1'
