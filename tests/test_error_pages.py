"""Browser-friendly error pages must not weaken API JSON semantics."""
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, Department, Employee, EmployeeCodeCounter
from app.security import hash_password


@pytest.fixture()
def error_page_client(monkeypatch):
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
                username="error-page-admin",
                password_hash=hash_password("Error-Page-Test-Password-123!"),
                active=True,
            ),
            Department(name="Error Page Test Department", active=True),
            EmployeeCodeCounter(id=1, last_number=0),
        ])
        db.commit()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def login(client):
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = client.post(
        "/login",
        data={
            "username": "error-page-admin",
            "password": "Error-Page-Test-Password-123!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_browser_404_gets_friendly_arabic_page(error_page_client):
    login(error_page_client)
    response = error_page_client.get(
        "/definitely-missing-page", headers={"Accept": "text/html"}
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "الصفحة غير موجودة" in response.text
    assert "العودة للرئيسية" in response.text


def test_api_404_stays_json(error_page_client):
    login(error_page_client)
    response = error_page_client.get("/definitely-missing-page")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


def test_browser_422_gets_friendly_arabic_page(error_page_client):
    login(error_page_client)
    response = error_page_client.get(
        "/staff-actions", params={"page": "abc"}, headers={"Accept": "text/html"}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "البيانات غير مكتملة أو غير صحيحة" in response.text


def test_api_422_keeps_structured_localized_json(error_page_client):
    login(error_page_client)
    response = error_page_client.get("/staff-actions", params={"page": "abc"})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    detail = response.json()["detail"]
    assert detail and "loc" in detail[0] and "field" in detail[0] and "msg" in detail[0]


def test_error_page_translates_to_english(error_page_client):
    login(error_page_client)
    response = error_page_client.get(
        "/definitely-missing-page",
        headers={"Accept": "text/html"},
        cookies={"my_hr_language": "en"},
    )
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert "الصفحة غير موجودة" not in response.text


def test_logged_out_browser_still_redirects_to_login(error_page_client):
    response = error_page_client.get(
        "/staff-actions", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_logged_out_api_still_gets_json_401(error_page_client):
    response = error_page_client.get("/staff-actions")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_browser_404_keeps_app_authored_reason(error_page_client):
    login(error_page_client)
    response = error_page_client.get(
        "/employees/999999/photo", headers={"Accept": "text/html"}
    )
    assert response.status_code == 404
    assert "صورة الموظف غير موجودة" in response.text
    assert "الرابط غير صحيح" not in response.text


def test_browser_403_keeps_app_authored_reason(error_page_client):
    login(error_page_client)
    page = error_page_client.get("/staff-actions")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    response = error_page_client.post(
        "/security-roster/confirm",
        data={
            "csrf_token": token,
            "work_date": "2026-11-22",
            "stage_token": "bogus-token",
            "reviewed": "yes",
        },
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 403
    assert "انتهت صلاحية معاينة الملف. ارفع الملف مرة أخرى." in response.text
    assert "سجّل الدخول" not in response.text


def test_browser_403_csrf_shows_form_expired_reason(error_page_client):
    login(error_page_client)
    response = error_page_client.post(
        "/security-roster/confirm",
        data={"csrf_token": "wrong", "work_date": "2026-11-22", "stage_token": "x"},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 403
    assert "انتهت صلاحية النموذج" in response.text
    assert "سجّل الدخول" not in response.text


def test_api_403_still_gets_json(error_page_client):
    login(error_page_client)
    response = error_page_client.post(
        "/security-roster/confirm",
        data={"csrf_token": "wrong", "work_date": "2026-11-22", "stage_token": "x"},
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
