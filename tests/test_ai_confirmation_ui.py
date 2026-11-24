"""Owner-selected separate review then confirm flow, synthetic route tests."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.csrf import csrf_protect
from app.dependencies import require_system_admin


@pytest.fixture
def client(monkeypatch):
    admin=SimpleNamespace(username='synthetic',display_name='Synthetic',role='system_admin')
    main.app.dependency_overrides[require_system_admin]=lambda:admin
    main.app.dependency_overrides[csrf_protect]=lambda:None
    monkeypatch.setattr(main,'recent_memory',lambda *args:[])
    monkeypatch.setattr(main,'local_model_status',lambda *args:{'available':False,'message':'','model':'Local'})
    monkeypatch.setattr(main,'build_smart_scan',lambda *args:[])
    with TestClient(main.app) as client:
        yield client
    main.app.dependency_overrides.clear()


def test_approve_without_review_receipt_never_executes(client,monkeypatch):
    monkeypatch.setattr(main,'verify_review_receipt',lambda *args:False)
    monkeypatch.setattr(main,'approve_command',lambda *args,**kwargs:pytest.fail('review bypass'))
    response=client.post('/ai/approve',data={'token':'synthetic'})
    assert response.status_code==200


@pytest.mark.parametrize('field,expected',[('',False),('false',False),('true',False),('confirmed',True)])
def test_attestation_comes_only_from_exact_human_form_value(client,monkeypatch,field,expected):
    captured={}
    monkeypatch.setattr(main,'verify_review_receipt',lambda *args:True)
    def approve(*args,**kwargs):
        captured.update(kwargs)
        return 'Synthetic complete'
    monkeypatch.setattr(main,'approve_command',approve)
    response=client.post('/ai/approve',data={'token':'synthetic','review_receipt':'valid',
                                           'human_coverage_confirmed':field})
    assert response.status_code==200
    assert captured['human_coverage_confirmed'] is expected


def pending(reviewed=False):
    return {'token':'synthetic','review_receipt':'synthetic','reviewed':reviewed,
        'plan':{'action':'approve_attendance_month'},'summary':'',
        'old_values':{'status':'under_review'},'new_values':{'status':'approved'},'risks':[]}


def test_review_screen_contains_unchecked_human_checkbox(client,monkeypatch):
    monkeypatch.setattr(main,'review_command',lambda *args:pending(True))
    response=client.post('/ai/review',data={'token':'synthetic'})
    assert response.status_code==200
    import re
    checkbox=re.search(r'<input[^>]+name="human_coverage_confirmed"[^>]*>',response.text)
    assert checkbox is not None
    assert ' checked' not in checkbox.group()
    assert 'required' in checkbox.group()
    assert 'name="review_receipt"' in response.text



def test_initial_preview_has_review_action_not_execute_action():
    request=SimpleNamespace(state=SimpleNamespace(language='ar',admin_role='system_admin'),
                            url=SimpleNamespace(path='/ai'))
    html=main.templates.env.get_template('ai.html').render(request=request,pending=pending(),
        admin='Synthetic',csrf_token='synthetic',status={'available':False,'model':'Local','message':''},
        scan=[],question='',answer=None,memory=[],ui_language='ar')
    assert 'action="/ai/review"' in html
    assert 'action="/ai/approve"' not in html


@pytest.mark.parametrize('language',['en','zh'])
def test_confirmation_interface_has_no_arabic_labels_in_other_locales(language):
    import re

    from app.i18n import translate_html
    request=SimpleNamespace(state=SimpleNamespace(language=language,admin_role='system_admin'),
                            url=SimpleNamespace(path='/ai'))
    html=main.templates.env.get_template('ai.html').render(request=request,pending=pending(True),
        admin='Synthetic',csrf_token='synthetic',status={'available':False,'model':'Local','message':''},
        scan=[],question='',answer=None,memory=[],ui_language=language)
    html=translate_html(html,language)
    section=html[html.index('id="ai-exact-review"'):]
    section=section[:section.index('</section>')]
    assert re.search(r'[\u0600-\u06ff]',section) is None
