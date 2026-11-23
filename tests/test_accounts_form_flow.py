"""Item 88 closure: the RENDERED accounts-page forms must actually submit.

Existing route tests POST to hand-written URLs, so a template with a wrong
action or field name could pass them. These tests parse the real HTML forms
(action + input names/values) from the served page and submit exactly that
payload: deactivate/activate, reset-password (with reauth), a CSRF-missing
negative, and a finance-role negative.
"""
import re
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Department, EmployeeCodeCounter, LeaveType
from app.security import hash_password

ADMIN_PW = "FormFlow-Admin-Pass-123!"
TARGET_PW = "FormFlow-Target-Pass-123!"


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._current = {"action": attrs.get("action", ""), "inputs": {}}
        elif tag == "input" and self._current is not None:
            name = attrs.get("name")
            if name:
                self._current["inputs"][name] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


def _rendered_forms(response):
    parser = _FormParser()
    parser.feed(response.text)
    return parser.forms


@pytest.fixture()
def form_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add_all([
            AdminAccount(
                username="form-admin",
                password_hash=hash_password(ADMIN_PW),
                display_name="مدير النماذج",
                role="system_admin",
                active=True,
            ),
            AdminAccount(
                username="form-target",
                password_hash=hash_password(TARGET_PW),
                display_name="حساب الهدف",
                role="finance",
                active=True,
            ),
            AdminAccount(
                username="form-finance",
                password_hash=hash_password(TARGET_PW),
                display_name="مالي بلا صلاحية",
                role="finance",
                active=True,
            ),
            Department(name="Form Flow Department", active=True),
            EmployeeCodeCounter(id=1, last_number=0),
            LeaveType(code="ANNUAL", name_ar="سنوية", name_en="Annual", is_paid=True),
        ])
        db.commit()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as client:
            client.test_session_factory = factory
            yield client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client, username, password):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _target_state(client):
    with client.test_session_factory() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "form-target"))
        return account


def _form_for(forms, suffix):
    for form in forms:
        if form["action"].endswith(suffix):
            return form
    raise AssertionError(f"no rendered form ends with {suffix}: {[f['action'] for f in forms]}")


def test_rendered_deactivate_activate_reset_forms_submit(form_client):
    client = form_client
    _login(client, "form-admin", ADMIN_PW)
    target = _target_state(client)

    page = client.get("/settings/accounts")
    assert page.status_code == 200
    forms = _rendered_forms(page)

    # Deactivate via the rendered form's own action + fields.
    deactivate = _form_for(forms, f"/settings/accounts/{target.id}/deactivate")
    assert "csrf_token" in deactivate["inputs"] and deactivate["inputs"]["csrf_token"]
    assert "reason" in deactivate["inputs"]
    payload = {k: v for k, v in deactivate["inputs"].items()}
    payload["reason"] = "إيقاف تجريبي"
    response = client.post(deactivate["action"], data=payload, follow_redirects=False)
    assert response.status_code == 303, response.text[:200]
    assert _target_state(client).active is False

    # The row now renders the activate form (item 88 expandable row intact).
    page = client.get("/settings/accounts")
    activate = _form_for(_rendered_forms(page), f"/settings/accounts/{target.id}/activate")
    payload = {k: v for k, v in activate["inputs"].items()}
    payload["reason"] = "إعادة تفعيل تجريبية"
    response = client.post(activate["action"], data=payload, follow_redirects=False)
    assert response.status_code == 303, response.text[:200]
    assert _target_state(client).active is True

    # Reset-password via the rendered form, with the reauth field it renders.
    page = client.get("/settings/accounts")
    reset = _form_for(_rendered_forms(page), f"/settings/accounts/{target.id}/reset-password")
    assert "current_password" in reset["inputs"], "reauth field must render in the reset form"
    payload = {k: v for k, v in reset["inputs"].items()}
    payload["reason"] = "إعادة تعيين تجريبية"
    payload["current_password"] = ADMIN_PW
    response = client.post(reset["action"], data=payload, follow_redirects=False)
    # Success renders the accounts page with the new one-time password shown.
    assert response.status_code == 200, response.text[:200]
    target = _target_state(client)
    assert target.must_change_password is True
    assert target.pending_credential_reset is False  # normal account: reset does not lock
    assert target.active is True
    match = re.search(r'كلمة مرور جديدة لحساب.*?<code>([^<]+)</code>', response.text, re.S)
    assert match, "the one-time password must be shown once on the rendered page"
    _login(client, "form-target", match.group(1).strip())  # it actually signs in


def test_rendered_form_without_csrf_rejected(form_client):
    client = form_client
    _login(client, "form-admin", ADMIN_PW)
    target = _target_state(client)
    page = client.get("/settings/accounts")
    deactivate = _form_for(_rendered_forms(page), f"/settings/accounts/{target.id}/deactivate")
    payload = {k: v for k, v in deactivate["inputs"].items() if k != "csrf_token"}
    payload["reason"] = "بدون رمز"
    response = client.post(deactivate["action"], data=payload, follow_redirects=False)
    assert response.status_code in (400, 403)
    assert _target_state(client).active is True


def test_finance_role_cannot_submit_rendered_form(form_client):
    client = form_client
    _login(client, "form-finance", TARGET_PW)
    page = client.get("/settings/accounts", follow_redirects=False)
    assert page.status_code == 403
    target = _target_state(client)
    response = client.post(
        f"/settings/accounts/{target.id}/deactivate",
        data={"csrf_token": "forged", "reason": "محاولة بلا صلاحية"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert _target_state(client).active is True
