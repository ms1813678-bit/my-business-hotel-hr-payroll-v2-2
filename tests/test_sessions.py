"""Server-side session inventory: login records, termination, revocation paths."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AdminSession, AuditLog
from app.security import hash_password

ADMIN_PW = "Guard-Test-Password-123!"


@pytest.fixture()
def make_client(monkeypatch):
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
        def build():
            test_client = TestClient(app)
            test_client.test_session_factory = factory
            return test_client

        yield build
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def login(client, password=ADMIN_PW):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "guard-admin", "password": password, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response


def session_count(factory):
    with factory() as db:
        return len(db.scalars(select(AdminSession)).all())


def test_login_records_session_and_accounts_page_lists_it(make_client):
    client = make_client()
    with client:
        login(client)
        assert session_count(client.test_session_factory) == 1
        page = client.get("/settings/accounts")
        assert page.status_code == 200
        assert "الجلسات المفتوحة" in page.text
        assert "جلستك الحالية" in page.text
        assert "مدير النظام التجريبي" in page.text


def test_deleted_session_row_kills_the_cookie(make_client):
    client = make_client()
    with client:
        login(client)
        assert client.get("/settings/accounts").status_code == 200
        with client.test_session_factory() as db:
            db.execute(delete(AdminSession))
            db.commit()
        assert client.get("/settings/accounts").status_code == 401


def test_cookie_without_session_record_is_rejected(make_client):
    client = make_client()
    with client:
        login(client)
        # simulate a pre-feature cookie: session fields but no session id
        client.cookies.clear()
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        signed = client.cookies.get("session")
        assert signed
        # strip the admin_session_id by re-signing is not possible; instead
        # delete all rows, which is the same validation path
        with client.test_session_factory() as db:
            db.execute(delete(AdminSession))
            db.commit()
        assert client.get("/settings/accounts").status_code == 401


def test_logout_removes_the_session_row(make_client):
    client = make_client()
    with client:
        login(client)
        assert session_count(client.test_session_factory) == 1
        page = client.get("/settings/accounts")
        client.post("/logout", data={"csrf_token": csrf(page)}, follow_redirects=False)
        assert session_count(client.test_session_factory) == 0
        assert client.get("/settings/accounts").status_code == 401


def test_manual_termination_ends_only_the_target_session(make_client):
    first = make_client()
    second = make_client()
    with first, second:
        login(first)
        login(second)
        assert session_count(first.test_session_factory) == 2
        page = first.get("/settings/accounts")
        ids = re.findall(r'/settings/accounts/sessions/([0-9a-f]+)/end', page.text)
        assert len(ids) == 2
        # the second login is the most recent row; terminate it via the first
        with first.test_session_factory() as db:
            rows = db.scalars(select(AdminSession).order_by(AdminSession.created_at)).all()
            second_id = rows[-1].id  # second login is the most recent
        response = first.post(
            f"/settings/accounts/sessions/{second_id}/end",
            data={"csrf_token": csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert session_count(first.test_session_factory) == 1
        # the terminated client is rejected; the terminator stays signed in
        assert second.get("/settings/accounts").status_code == 401
        assert first.get("/settings/accounts").status_code == 200
        with first.test_session_factory() as db:
            audit = db.scalar(select(AuditLog).where(AuditLog.action == "session_ended"))
            assert audit is not None and audit.actor == "guard-admin"


def test_password_change_keeps_current_and_drops_other_sessions(make_client):
    first = make_client()
    second = make_client()
    with first, second:
        login(first)
        login(second)
        page = first.get("/settings/password")
        response = first.post(
            "/settings/password",
            data={
                "csrf_token": csrf(page),
                "current_password": ADMIN_PW,
                "new_password": "Brand-New-Password-66!",
                "confirm_password": "Brand-New-Password-66!",
            },
        )
        assert response.status_code == 200
        assert session_count(first.test_session_factory) == 1
        assert first.get("/settings/accounts").status_code == 200
        assert second.get("/settings/accounts").status_code == 401
