"""Telegram alert settings require the administrator's own password."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount
from app.security import hash_password
from app.telegram_alerts import get_config

ADMIN_PW = "Guard-Test-Password-123!"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add(AdminAccount(
            username="guard-admin",
            password_hash=hash_password(ADMIN_PW),
            display_name="مدير النظام التجريبي",
            role="system_admin",
            active=True,
        ))
        db.commit()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as test_client:
            test_client.test_session_factory = factory
            yield test_client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


@pytest.fixture()
def admin_client(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def tg_post(client, path, password, **fields):
    page = client.get("/settings/password")
    data = {"csrf_token": csrf(page), "current_password": password}
    data.update(fields)
    return client.post(path, data=data, follow_redirects=False)


def test_settings_save_requires_current_password(admin_client):
    wrong = tg_post(admin_client, "/settings/telegram", "wrong-password", bot_token="123:abc")
    assert wrong.status_code == 303
    assert wrong.headers["location"] == "/settings/password?tg=auth"
    with admin_client.test_session_factory() as db:
        assert get_config(db)["bot_token"] == "", "wrong password must not save anything"

    missing = tg_post(admin_client, "/settings/telegram", "", bot_token="123:abc")
    assert missing.headers["location"] == "/settings/password?tg=auth"

    # correct password passes reauth (bad token proves the route proceeded)
    ok = tg_post(admin_client, "/settings/telegram", ADMIN_PW, bot_token="123:abc")
    assert ok.headers["location"] == "/settings/password?tg=bad_token"


@pytest.mark.parametrize("path,expected_after_reauth", [
    ("/settings/telegram/bind-link", "/settings/password?tg=need_token"),
    ("/settings/telegram/bind-confirm", "/settings/password?tg=need_token"),
    ("/settings/telegram/test", "/settings/password?tg=test_failed"),
])
def test_bind_and_test_routes_require_current_password(admin_client, path, expected_after_reauth):
    wrong = tg_post(admin_client, path, "wrong-password")
    assert wrong.status_code == 303
    assert wrong.headers["location"] == "/settings/password?tg=auth"

    ok = tg_post(admin_client, path, ADMIN_PW)
    assert ok.headers["location"] == expected_after_reauth


def test_password_page_shows_reauth_field_and_error(admin_client):
    page = admin_client.get("/settings/password")
    assert 'name="current_password"' in page.text
    page = admin_client.get("/settings/password?tg=auth")
    assert "كلمة مرورك غير صحيحة" in page.text
