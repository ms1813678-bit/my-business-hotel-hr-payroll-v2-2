import re
from types import SimpleNamespace
from fastapi import Request
from fastapi.testclient import TestClient
import pytest
from app import main
from app.dependencies import get_current_admin
from app.i18n import translate_html


@pytest.fixture
def settings_client():
    identity=SimpleNamespace(role='system_admin',username='synthetic',must_change_password=False)
    def current(request: Request):
        request.state.admin_role=identity.role
        return identity
    main.app.dependency_overrides[get_current_admin]=current
    try:
        with TestClient(main.app) as client:yield client,identity
    finally:main.app.dependency_overrides.clear()


def test_settings_authentication_required():
    with TestClient(main.app) as client:
        assert client.get('/settings',follow_redirects=False).status_code in (401,303)


def test_settings_role_visibility_no_mutating_forms(settings_client):
    client,identity=settings_client
    for role in ('system_admin','assistant_manager','finance'):
        identity.role=role;page=client.get('/settings');assert page.status_code==200
        content=page.text.split('<main class="content">')[1].split('<footer>')[0]
        assert '/settings/password' in content
        assert ('/settings/accounts' in content)==(role=='system_admin')
        assert ('/settings/backup' in content)==(role=='system_admin')
        assert ('/settings/audit' in content)==(role=='system_admin')
        assert ('telegram-settings' in content)==(role=='system_admin')
        assert 'method="post"' not in content
        assert 'language-switch' not in page.text


def test_login_has_compact_access_and_native_language_names():
    with TestClient(main.app) as client:
        page=client.get('/login').text
        assert 'language-picker' in page and 'login-language' not in page
        assert all('/language/'+code in page for code in ('ar','en','zh'))
        for lang in ('ar','en','zh'):
            localized=translate_html(page,lang)
            assert 'English' in localized and '中文' in localized and 'العربية' in localized


@pytest.mark.parametrize('language',['en','zh'])
def test_settings_labels_localize_except_explicit_native_picker(settings_client,language):
    from html.parser import HTMLParser
    client,_=settings_client;client.cookies.set('my_hr_language',language)
    content=client.get('/settings').text.split('<main class="content">')[1].split('<footer>')[0]
    content=re.sub(r'<a[^>]*data-no-translate[^>]*>.*?</a>','',content,flags=re.S)
    assert not re.search(r'[\u0600-\u06ff]',content)


from tests.test_roles import role_client, login, ADMIN_PW  # noqa: E402,F401


def test_settings_hub_does_not_bypass_forced_rotation_or_restore_gate(role_client):
    from sqlalchemy import select
    from app.models import AdminAccount,SystemSetting
    client=role_client;login(client,'guard-admin',ADMIN_PW)
    with client.test_session_factory() as db:
        admin=db.scalar(select(AdminAccount).where(AdminAccount.username=='guard-admin'))
        admin.must_change_password=True;db.commit()
    response=client.get('/settings',headers={'accept':'text/html'},follow_redirects=False)
    assert response.status_code==303 and '/settings/password' in response.headers['location']
    with client.test_session_factory() as db:
        admin=db.scalar(select(AdminAccount).where(AdminAccount.username=='guard-admin'))
        admin.must_change_password=False
        db.merge(SystemSetting(key='post_restore_regen_required',value='1',updated_by='synthetic'));db.commit()
    response=client.get('/settings',headers={'accept':'text/html'},follow_redirects=False)
    assert response.status_code==303 and '/settings/accounts' in response.headers['location']
