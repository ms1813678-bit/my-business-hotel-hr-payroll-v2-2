"""Disaster-restore gate on /login/recover: while the post-restore passphrase
flag is armed, recovery codes alone (which came from the archive) cannot reset
the admin password; the offline backup passphrase is required too. Normal
recovery without the flag is unaffected."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AuditLog, RecoveryCode, SystemSetting
from app.security import hash_password, verify_password

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
    # Offline passphrase proof: only "correct-pass" unwraps the data key.
    import app.recovery_login_proof as login_proof
    def fake_unwrap(db, passphrase):
        if passphrase != "correct-pass":
            raise ValueError("wrong_passphrase")
        return b"data-key"
    monkeypatch.setattr(login_proof, "unwrap_data_key_for_restore", fake_unwrap)
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


def login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def generate_codes(client):
    page = client.get("/settings/accounts")
    response = client.post(
        "/settings/accounts/recovery-codes",
        data={"csrf_token": csrf(page), "current_password": ADMIN_PW},
        follow_redirects=False,
    )
    assert response.status_code == 200
    return re.findall(r"<code>([^<]+)</code>", response.text)


def recover(client, username, code, passphrase=""):
    page = client.get("/login/recover")
    return client.post(
        "/login/recover",
        data={
            "csrf_token": csrf(page),
            "username": username,
            "recovery_code": code,
            "new_password": "Recovered-New-Password-77!",
            "confirm_password": "Recovered-New-Password-77!",
            "backup_passphrase": passphrase,
        },
        follow_redirects=False,
    )


def arm_flag(client):
    with client.test_session_factory() as db:
        db.merge(SystemSetting(key="post_restore_passphrase_required", value="1", updated_by="test"))
        db.commit()


def code_unused(client, code):
    with client.test_session_factory() as db:
        row = db.scalar(select(RecoveryCode).where(RecoveryCode.used_at.is_(None)))
        return row is not None and verify_password(code, row.code_hash)


def test_armed_recovery_without_passphrase_cannot_reset_password(client):
    login(client)
    codes = generate_codes(client)
    arm_flag(client)
    response = recover(client, "guard-admin", codes[0])
    assert response.status_code == 401
    assert "كلمة مرور النسخ الاحتياطية غير صحيحة." in response.text
    assert code_unused(client, codes[0])


def test_armed_recovery_wrong_passphrase_keeps_code_and_password(client):
    login(client)
    codes = generate_codes(client)
    arm_flag(client)
    response = recover(client, "guard-admin", codes[0], passphrase="wrong-pass")
    assert response.status_code == 401
    assert code_unused(client, codes[0])
    with client.test_session_factory() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        assert verify_password(ADMIN_PW, account.password_hash)


def test_armed_recovery_with_passphrase_succeeds_and_disarms(client):
    login(client)
    codes = generate_codes(client)
    arm_flag(client)
    response = recover(client, "guard-admin", codes[0], passphrase="correct-pass")
    assert response.status_code == 303
    with client.test_session_factory() as db:
        flag = db.get(SystemSetting, "post_restore_passphrase_required")
        assert flag is not None and flag.value == ""
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        assert verify_password("Recovered-New-Password-77!", account.password_hash)
        audit = db.scalar(
            select(AuditLog).where(AuditLog.action == "post_restore_passphrase_verified")
        )
        assert audit is not None
        assert "passphrase" in (audit.reason or "").lower()


def test_unarmed_recovery_needs_no_passphrase(client):
    login(client)
    codes = generate_codes(client)
    response = recover(client, "guard-admin", codes[0])
    assert response.status_code == 303
    with client.test_session_factory() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        assert verify_password("Recovered-New-Password-77!", account.password_hash)


def test_armed_recover_page_requests_passphrase(client):
    arm_flag(client)
    page = client.get("/login/recover")
    assert page.status_code == 200
    assert 'name="backup_passphrase"' in page.text
