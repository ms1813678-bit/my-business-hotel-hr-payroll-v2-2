"""Backup administration routes require the single HR administrator and CSRF."""
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import AdminAccount, BackupRun
from app.security import hash_password
from app.backup_crypto import set_passphrase


@pytest.fixture()
def backup_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add(AdminAccount(
            username="backup-admin",
            password_hash=hash_password("Backup-Test-Password-123!"),
            active=True,
        ))
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


def csrf(response):
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def login(client):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "username": "backup-admin",
            "password": "Backup-Test-Password-123!",
            "csrf_token": csrf(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_backup_page_and_mutations_are_admin_and_csrf_protected(backup_client):
    client = backup_client
    assert client.get("/settings/backup").status_code == 401
    assert client.post("/settings/backup/create", data={}).status_code == 403
    login(client)
    page = client.get("/settings/backup")
    assert page.status_code == 200
    # Owner decision (November 2026): no automatic retention deletion at all.
    assert "40 يومًا" not in page.text
    assert "365 يومًا" not in page.text
    assert "لا تُحذف أي نسخة تلقائيًا" in page.text


def test_manual_backup_without_external_drive_is_recorded_as_failed(backup_client):
    client = backup_client
    login(client)
    with client.test_session_factory() as db:
        set_passphrase(db, "synthetic-backup-passphrase", actor="test")
        db.commit()
    token = csrf(client.get("/settings/backup"))
    response = client.post(
        "/settings/backup/create",
        data={"csrf_token": token, "backup_type": "manual"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=backup" in response.headers["location"]
    with client.test_session_factory() as db:
        run = db.scalar(select(BackupRun))
        assert run.status == "failed"
        assert "external backup drive" in run.error_message


def test_restore_requires_explicit_phrase_and_reason(backup_client):
    client = backup_client
    login(client)
    token = csrf(client.get("/settings/backup"))
    response = client.post(
        "/settings/backup/restore",
        data={
            "csrf_token": token,
            "backup_id": "1",
            "confirm_text": "WRONG",
            "reason": "Synthetic restore validation",
            "current_password": "Backup-Test-Password-123!",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=confirm" in response.headers["location"]


def test_manual_backup_requires_configured_encryption(backup_client):
    client = backup_client
    login(client)
    token = csrf(client.get("/settings/backup"))
    response = client.post(
        "/settings/backup/create",
        data={"csrf_token": token, "backup_type": "manual"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=passphrase_required" in response.headers["location"]
    with client.test_session_factory() as db:
        assert db.scalar(select(BackupRun)) is None


def test_restore_requires_configured_encryption(backup_client):
    client = backup_client
    login(client)
    token = csrf(client.get("/settings/backup"))
    response = client.post(
        "/settings/backup/restore",
        data={
            "csrf_token": token,
            "backup_id": "1",
            "confirm_text": "RESTORE",
            "reason": "Synthetic restore validation",
            "current_password": "Backup-Test-Password-123!",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=passphrase_required" in response.headers["location"]


def _fabricate_backup_archive(path, backup_type, created_at):
    import hashlib as hashlib_mod
    import json as json_mod
    import zipfile

    database_dump = b"synthetic PostgreSQL custom-format dump bytes"
    manifest = {
        "format_version": 1,
        "application": "M.Y. Business Hotel HR & Payroll System",
        "backup_type": backup_type,
        "created_at": created_at.isoformat(),
        "database_sha256": hashlib_mod.sha256(database_dump).hexdigest(),
        "employee_photos_sha256": hashlib_mod.sha256(b"").hexdigest(),
        "employee_photo_count": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("database.dump", database_dump)
        archive.writestr("manifest.json", json_mod.dumps(manifest))
    return path


def _standalone_record_and_key(passphrase):
    from sqlalchemy.orm import Session as _Session

    from app.backup_crypto import export_public_record, load_data_key, set_passphrase

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with _Session(engine) as db:
        set_passphrase(db, passphrase, actor="test")
        db.commit()
        record = export_public_record(db)
        key = load_data_key(db)
    engine.dispose()
    return record, key


def test_restore_route_uses_preserved_key_record(backup_client, tmp_path, monkeypatch):
    """Full HTTP restore path: the install's own record cannot open another
    install's archive; the preserved record on the shared drive can."""
    import hashlib as hashlib_mod
    import json as jsonlib
    from datetime import datetime, timezone
    from pathlib import Path

    from app import backup_workflow
    from app.backup_crypto import encrypt_file
    from app.backup_workflow import sha256_file
    from app.models import SystemSetting

    client = backup_client
    factory = client.test_session_factory
    backup_root = tmp_path / "shared-backups"
    backup_root.mkdir()

    record_a, key_a = _standalone_record_and_key("synthetic-passphrase-alice")
    plain = _fabricate_backup_archive(
        tmp_path / "plain.zip", "manual", datetime(2026, 11, 7, tzinfo=timezone.utc)
    )
    archive = backup_root / "manual" / "archived.zip.enc"
    archive.parent.mkdir(parents=True, exist_ok=True)
    encrypt_file(key_a, plain, archive)

    # Target install B: own passphrase (required by the route gate), shared root.
    with factory.begin() as db:
        set_passphrase(db, "synthetic-passphrase-bob", actor="backup-admin")
        db.add(SystemSetting(key="backup_root", value=str(backup_root), updated_by="backup-admin"))
        run = BackupRun(
            backup_type="manual",
            status="success",
            archive_path=str(archive),
            sha256=sha256_file(archive),
            size_bytes=archive.stat().st_size,
            created_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
            initiated_by="backup-admin",
        )
        db.add(run)
        db.flush()
        backup_id = run.id

    # Shared drive state: canonical holds B's record; A's record is preserved.
    record_b, _ = _standalone_record_and_key("synthetic-passphrase-bob")
    (backup_root / "backup_encryption_key.json").write_text(jsonlib.dumps(record_b), encoding="utf-8")
    digest = hashlib_mod.sha256(record_a["wrapped_key"].encode("utf-8")).hexdigest()[:8]
    (backup_root / f"backup_encryption_key.prev-{digest}.json").write_text(
        jsonlib.dumps(record_a), encoding="utf-8"
    )

    snap = _fabricate_backup_archive(
        backup_root / "pre_restore" / "snap.zip", "pre_restore",
        datetime(2026, 11, 8, tzinfo=timezone.utc),
    )

    def fake_create_backup(db, backup_type, actor):
        run = BackupRun(
            backup_type=backup_type,
            status="success",
            archive_path=str(snap),
            sha256=sha256_file(snap),
            size_bytes=snap.stat().st_size,
            created_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
            initiated_by=actor,
        )
        db.add(run)
        db.flush()
        return run

    captured = []

    def fake_restore(path, root, expected_sha256=None, data_key=None, **kwargs):
        captured.append((Path(path).name, data_key))

    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(backup_workflow, "create_backup", fake_create_backup)
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_restore)

    login(client)
    token = csrf(client.get("/settings/backup"))
    response = client.post(
        "/settings/backup/restore",
        data={
            "csrf_token": token,
            "backup_id": str(backup_id),
            "confirm_text": "RESTORE",
            "reason": "Synthetic HTTP restore via preserved key record",
            "passphrase": "synthetic-passphrase-alice",
            "current_password": "Backup-Test-Password-123!",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?restored=1"
    # The preserved record's key opened the archive, not the canonical record's.
    assert captured[0] == (archive.name, key_a)
