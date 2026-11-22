"""Post-restore reconciliation: sessions dropped, forced rotation, regen gate."""
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import backup_workflow
from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, AdminSession, AuditLog, SystemSetting
from app.security import hash_password

ADMIN_PW = "Guard-Test-Password-123!"


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add(AdminAccount(username="guard-admin", password_hash=hash_password(ADMIN_PW),
                            display_name="مدير النظام التجريبي", role="system_admin", active=True))
        db.add(AdminAccount(username="finance-demo", password_hash=hash_password(ADMIN_PW),
                            display_name="مالية تجريبية", role="finance", active=True))
        db.add(AdminSession(id="live-session-1", admin_account_id=1,
                            created_at=datetime.now(timezone.utc),
                            last_seen_at=datetime.now(timezone.utc)))
        db.add(SystemSetting(key="backup_root", value="/tmp/exercise", updated_by="test"))
        db.commit()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr("app.main.SessionLocal", factory)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_restore_drops_sessions_and_forces_rotation_and_regen(factory, monkeypatch, tmp_path):
    """Regression proof for the restore boundary: after any successful restore,
    no session row survives (old cookies die even with an unchanged signing
    secret), every account must rotate its password at next login, and the
    sysadmin faces the code-regeneration gate."""
    monkeypatch.setattr(backup_workflow, "verify_backup_archive", lambda *a, **k: {"created_at": "2026-11-23T00:00:00+00:00"})
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", lambda *a, **k: None)

    class _Run:
        status = "success"
        archive_path = str(tmp_path / "pre.zip")
        id = 1
        sha256 = "0" * 64

    def _fake_backup(db, t, a):
        (tmp_path / "pre.zip").write_bytes(b"pre")
        return _Run()
    monkeypatch.setattr(backup_workflow, "create_backup", _fake_backup)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"zip")
    backup_root = tmp_path
    with factory() as db:
        db.merge(SystemSetting(key="backup_root", value=str(backup_root), updated_by="test"))
        db.commit()

    log = backup_workflow.restore_backup(
        archive, "ab" * 32, actor="guard-admin", reason="اختبار",
    )
    assert log["restored_at"]

    with factory() as db:
        assert db.scalars(select(AdminSession)).all() == [], "restore must drop every session"
        accounts = db.scalars(select(AdminAccount)).all()
        assert all(a.must_change_password for a in accounts), "every account rotates at next login"
        flag = db.get(SystemSetting, "post_restore_regen_required")
        assert flag is not None and flag.value, "regeneration gate must be armed"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "backup_restored")) is not None


def _csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_sysadmin_confined_until_codes_regenerated(factory, monkeypatch):
    with factory() as db:
        db.merge(SystemSetting(key="post_restore_regen_required",
                               value=datetime.now(timezone.utc).isoformat(), updated_by="test"))
        db.commit()

    with TestClient(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        # any unrelated page redirects to the accounts reconciliation
        other = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        assert other.status_code == 303
        assert other.headers["location"] == "/settings/accounts?error=regen_required"
        page = client.get("/settings/accounts")
        assert "مطلوب بعد استرجاع النسخة الاحتياطية" in page.text
        # regenerating clears the gate
        response = client.post(
            "/settings/accounts/recovery-codes",
            data={"csrf_token": _csrf(page), "current_password": ADMIN_PW},
            follow_redirects=False,
        )
        assert response.status_code == 200
        with factory() as db:
            flag = db.get(SystemSetting, "post_restore_regen_required")
            assert flag is not None and not flag.value
        assert client.get("/", follow_redirects=False).status_code == 200
