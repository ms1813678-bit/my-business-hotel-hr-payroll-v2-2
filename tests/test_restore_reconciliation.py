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


def test_disaster_restore_drops_sessions_and_forces_rotation_and_regen(factory, monkeypatch, tmp_path):
    """Disaster path (nothing to carry over, e.g. fresh install): after the
    restore, no session row survives, every account must rotate its password
    at next login, and the sysadmin faces the code-regeneration gate."""
    monkeypatch.setattr(backup_workflow, "_capture_security_state",
                        lambda db: {"accounts": {}, "codes": [], "settings": {}})
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


def test_live_restore_carries_credentials_forward(factory, monkeypatch, tmp_path):
    """Live-database restore: the archive's old passwords and consumed codes
    must NOT come back. Current password hashes, rotation flags, code
    consumption and throttle counters survive the restore; sessions die."""
    from app.models import RecoveryCode

    with factory() as db:
        admin = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        db.add(RecoveryCode(admin_account_id=admin.id, code_hash="hash-used", code_lookup="lk1",
                            created_at=datetime.now(timezone.utc), used_at=datetime.now(timezone.utc)))
        db.add(RecoveryCode(admin_account_id=admin.id, code_hash="hash-fresh", code_lookup="lk2",
                            created_at=datetime.now(timezone.utc)))
        db.merge(SystemSetting(key="reauth_security",
                               value='{"failed_attempts": 3, "locked_until": null}', updated_by="test"))
        db.commit()
        original_hash = admin.password_hash

    monkeypatch.setattr(backup_workflow, "verify_backup_archive", lambda *a, **k: {"created_at": "2026-11-23T00:00:00+00:00"})

    def fake_restore(*a, **k):
        # the archive replaces the data: old password hash, rotation flag off,
        # consumed code resurrected as unused, extra codes deleted
        with factory.begin() as db:
            admin = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
            admin.password_hash = "archive-time-old-hash"
            admin.must_change_password = False
            for row in db.scalars(select(RecoveryCode)).all():
                row.used_at = None
                row.code_hash = "archive-time-code-hash"

    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_restore)

    class _Run:
        status = "success"
        id = 1
        sha256 = "0" * 64

    def _fake_backup(db, t, a):
        (tmp_path / "pre.zip").write_bytes(b"pre")
        run = _Run()
        run.archive_path = str(tmp_path / "pre.zip")
        return run

    monkeypatch.setattr(backup_workflow, "create_backup", _fake_backup)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"zip")
    with factory() as db:
        db.merge(SystemSetting(key="backup_root", value=str(tmp_path), updated_by="test"))
        db.commit()

    log = backup_workflow.restore_backup(archive, "ab" * 32, actor="guard-admin", reason="اختبار")
    assert log["restored_at"]

    with factory() as db:
        assert db.scalars(select(AdminSession)).all() == [], "restore must drop every session"
        admin = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        assert admin.password_hash == original_hash, "current password survives; archive password must not return"
        assert admin.must_change_password is False, "no forced rotation when nothing rolled back"
        codes = {c.code_lookup: c for c in db.scalars(select(RecoveryCode)).all()}
        assert codes["lk1"].used_at is not None, "consumed code must stay consumed"
        assert codes["lk1"].code_hash == "hash-used"
        assert codes["lk2"].code_hash == "hash-fresh"
        flag = db.get(SystemSetting, "post_restore_regen_required")
        assert flag is None or not flag.value or flag.value == "", "no regen gate when nothing rolled back"
        assert db.get(SystemSetting, "reauth_security").value == '{"failed_attempts": 3, "locked_until": null}'
        actions = [a.action for a in db.scalars(select(AuditLog)).all()]
        assert "backup_restored" in actions
        assert "restore_credentials_carried_over" in actions


def test_restore_route_requires_current_password(factory, monkeypatch):
    """The restore route must not run on an unlocked session alone."""
    called = []
    monkeypatch.setattr("app.main.restore_backup", lambda *a, **k: called.append(1))

    with factory() as db:
        from app.models import BackupRun
        db.add(BackupRun(backup_type="manual", status="success", archive_path="/tmp/x.zip.enc",
                         sha256="ab" * 32, initiated_by="test"))
        db.commit()

    with TestClient(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "guard-admin", "password": ADMIN_PW, "csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get("/settings/backup")
        token = _csrf(page)
        response = client.post(
            "/settings/backup/restore",
            data={"csrf_token": token, "backup_id": "1", "confirm_text": "RESTORE",
                  "reason": "اختبار", "passphrase": "", "current_password": "wrong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/backup?error=auth"
        assert called == [], "restore must not start without the admin's current password"


def test_disaster_restore_arms_passphrase_gate_and_login_requires_it(factory, monkeypatch, tmp_path):
    """Disaster path with encrypted backups: restored old credentials alone
    must NOT sign anyone in — the offline backup passphrase is required once."""
    from app.backup_crypto import set_passphrase

    with factory() as db:
        set_passphrase(db, "synthetic-backup-passphrase", actor="test")
        db.commit()

    monkeypatch.setattr(backup_workflow, "_capture_security_state",
                        lambda db: {"accounts": {}, "codes": [], "settings": {}})
    monkeypatch.setattr(backup_workflow, "verify_backup_archive", lambda *a, **k: {"created_at": "2026-11-23T00:00:00+00:00"})
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", lambda *a, **k: None)

    class _Run:
        status = "success"
        id = 1
        sha256 = "0" * 64

    def _fake_backup(db, t, a):
        (tmp_path / "pre.zip").write_bytes(b"pre")
        run = _Run()
        run.archive_path = str(tmp_path / "pre.zip")
        return run

    monkeypatch.setattr(backup_workflow, "create_backup", _fake_backup)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"zip")
    with factory() as db:
        db.merge(SystemSetting(key="backup_root", value=str(tmp_path), updated_by="test"))
        db.commit()

    backup_workflow.restore_backup(archive, "ab" * 32, actor="guard-admin", reason="اختبار")
    with factory() as db:
        flag = db.get(SystemSetting, "post_restore_passphrase_required")
        assert flag is not None and flag.value, "disaster restore with encrypted backups must arm the passphrase gate"

    with TestClient(app) as client:
        page = client.get("/login")
        assert "كلمة مرور النسخ الاحتياطية" in page.text, "login page must ask for the passphrase"
        token = _csrf(page)

        # correct account password + WRONG backup passphrase: refused, no session
        response = client.post(
            "/login",
            data={"username": "guard-admin", "password": ADMIN_PW,
                  "backup_passphrase": "wrong-passphrase", "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert "كلمة مرور النسخ الاحتياطية غير صحيحة" in response.text
        assert "admin_username" not in client.cookies.get("session", "")

        # correct account password alone: refused (field required by server too)
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "guard-admin", "password": ADMIN_PW,
                  "backup_passphrase": "", "csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 401

        # correct password + correct passphrase: in, gate cleared, audited
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "guard-admin", "password": ADMIN_PW,
                  "backup_passphrase": "synthetic-backup-passphrase", "csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with factory() as db:
            flag = db.get(SystemSetting, "post_restore_passphrase_required")
            assert flag is not None and not flag.value
            assert db.scalar(select(AuditLog).where(AuditLog.action == "post_restore_passphrase_verified")) is not None
        page = client.get("/login")
        assert "كلمة مرور النسخ الاحتياطية" not in page.text


def test_disaster_restore_without_backup_passphrase_keeps_legacy_gate(factory, monkeypatch, tmp_path):
    """Unencrypted-archive disaster path: no offline proof exists, so the
    passphrase gate cannot arm; forced rotation + regen gate remain."""
    monkeypatch.setattr(backup_workflow, "_capture_security_state",
                        lambda db: {"accounts": {}, "codes": [], "settings": {}})
    monkeypatch.setattr(backup_workflow, "verify_backup_archive", lambda *a, **k: {"created_at": "2026-11-23T00:00:00+00:00"})
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", lambda *a, **k: None)

    class _Run:
        status = "success"
        id = 1
        sha256 = "0" * 64

    def _fake_backup(db, t, a):
        (tmp_path / "pre.zip").write_bytes(b"pre")
        run = _Run()
        run.archive_path = str(tmp_path / "pre.zip")
        return run

    monkeypatch.setattr(backup_workflow, "create_backup", _fake_backup)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"zip")
    with factory() as db:
        db.merge(SystemSetting(key="backup_root", value=str(tmp_path), updated_by="test"))
        db.commit()

    backup_workflow.restore_backup(archive, "ab" * 32, actor="guard-admin", reason="اختبار")
    with factory() as db:
        flag = db.get(SystemSetting, "post_restore_passphrase_required")
        assert flag is None or not flag.value, "no passphrase record: passphrase gate must not arm"
        assert all(a.must_change_password for a in db.scalars(select(AdminAccount)).all())


def test_live_restore_locks_archive_resurrected_accounts(factory, monkeypatch, tmp_path):
    """Owner-approved (2026-11-23): accounts that exist only in the archive —
    deleted after the backup, or restored onto a fresh install with just its
    bootstrap admin — come back LOCKED (inactive + forced rotation, no codes)
    until the current admin re-enables and resets them. Snapshot accounts are
    untouched."""
    from app.models import RecoveryCode

    monkeypatch.setattr(backup_workflow, "verify_backup_archive", lambda *a, **k: {"created_at": "2026-11-23T00:00:00+00:00"})

    def fake_restore(*a, **k):
        with factory.begin() as db:
            # The archive resurrects a deleted account with a live old password
            # and a previously consumed code re-usable again.
            old = AdminAccount(username="deleted-old-account", password_hash="archive-stolen-hash",
                               display_name="حساب قديم", role="finance", active=True,
                               must_change_password=False)
            db.add(old)
            db.flush()
            db.add(RecoveryCode(admin_account_id=old.id, code_hash="archive-code-hash",
                                code_lookup="lk-old", created_at=datetime.now(timezone.utc)))

    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_restore)

    class _Run:
        status = "success"
        id = 1
        sha256 = "0" * 64

    def _fake_backup(db, t, a):
        (tmp_path / "pre.zip").write_bytes(b"pre")
        run = _Run()
        run.archive_path = str(tmp_path / "pre.zip")
        return run

    monkeypatch.setattr(backup_workflow, "create_backup", _fake_backup)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"zip")
    with factory() as db:
        db.merge(SystemSetting(key="backup_root", value=str(tmp_path), updated_by="test"))
        db.commit()

    log = backup_workflow.restore_backup(archive, "ab" * 32, actor="guard-admin", reason="اختبار")
    assert log["restored_at"]

    with factory() as db:
        old = db.scalar(select(AdminAccount).where(AdminAccount.username == "deleted-old-account"))
        assert old.active is False, "archive-only account must come back locked"
        assert old.must_change_password is True, "and must rotate on re-enable"
        assert old.pending_credential_reset is True, "and plain re-activation must be refused"
        assert db.scalars(select(RecoveryCode).where(RecoveryCode.admin_account_id == old.id)).all() == [], \
            "archive codes must not resurrect"
        current = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        assert current.active is True and current.must_change_password is False
        assert current.pending_credential_reset is False
        finance = db.scalar(select(AdminAccount).where(AdminAccount.username == "finance-demo"))
        assert finance.active is True, "snapshot accounts are not locked"
        assert finance.pending_credential_reset is False
        actions = [a.action for a in db.scalars(select(AuditLog)).all()]
        assert "restore_resurrected_accounts_locked" in actions
        row = db.scalar(select(AuditLog).where(AuditLog.action == "restore_resurrected_accounts_locked"))
        assert "deleted-old-account" in (row.new_value or "")


def _patch_restore_scaffolding(monkeypatch, tmp_path):
    """Shared fakes: verified archive, captured pre-restore backup file."""
    monkeypatch.setattr(backup_workflow, "verify_backup_archive",
                        lambda *a, **k: {"created_at": "2026-11-23T00:00:00+00:00"})

    class _Run:
        status = "success"
        id = 1
        sha256 = "0" * 64

    def _fake_backup(db, t, a):
        (tmp_path / "pre.zip").write_bytes(b"pre")
        run = _Run()
        run.archive_path = str(tmp_path / "pre.zip")
        return run

    monkeypatch.setattr(backup_workflow, "create_backup", _fake_backup)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"zip")
    from app.db import SessionLocal as _SessionLocal  # picks up the factory monkeypatch
    with _SessionLocal() as db:
        db.merge(SystemSetting(key="backup_root", value=str(tmp_path), updated_by="test"))
        db.commit()
    return archive


def test_reapply_recreates_account_missing_from_archive(factory, tmp_path):
    """Snapshot account absent from the archive (created after the backup, or
    a fresh install's bootstrap admin): it must be recreated with its CURRENT
    credentials and its recovery codes re-linked to the new row id.
    Regression for the NOT NULL flush crash: the row must be fully populated
    before any flush, so this creation path can no longer crash mid-restore."""
    from app.models import RecoveryCode
    from app.security import verify_password

    with factory() as db:
        snapshot = backup_workflow._capture_security_state(db)
    snapshot["accounts"]["post-backup-admin"] = {
        "password_hash": hash_password("Post-Backup-Pass-999!"),
        "session_version": 2,
        "active": True,
        "must_change_password": False,
        "role": "system_admin",
        "display_name": "مدير لاحق للنسخة",
        "pending_credential_reset": False,
    }
    snapshot["codes"] = [{
        "username": "post-backup-admin",
        "code_hash": "hash-post",
        "code_lookup": "lkpost",
        "created_at": datetime.now(timezone.utc),
        "used_at": None,
    }]

    with factory.begin() as db:
        # The live DB does NOT contain post-backup-admin: this is exactly the
        # state after an archive without it replaced the schema.
        backup_workflow._reapply_security_state(db, snapshot, actor="test")

    with factory() as db:
        recreated = db.scalar(select(AdminAccount).where(AdminAccount.username == "post-backup-admin"))
        assert recreated is not None, "snapshot-only account must be recreated"
        assert verify_password("Post-Backup-Pass-999!", recreated.password_hash)
        assert recreated.session_version == 2
        assert recreated.active is True and recreated.must_change_password is False
        assert recreated.role == "system_admin"
        assert recreated.display_name == "مدير لاحق للنسخة"
        assert recreated.pending_credential_reset is False
        code = db.scalar(select(RecoveryCode).where(RecoveryCode.code_lookup == "lkpost"))
        assert code is not None, "snapshot codes must be recreated"
        assert code.admin_account_id == recreated.id, "code FK must map to the recreated row's new id"
        existing = db.scalar(select(AdminAccount).where(AdminAccount.username == "guard-admin"))
        assert existing is not None and verify_password(ADMIN_PW, existing.password_hash)


def test_finalize_failure_rolls_back_to_pre_restore(factory, monkeypatch, tmp_path):
    """Failure-atomicity: if ANY post-restore finalization step fails after
    the schema swap, the whole restore aborts and the database returns to the
    pre-restore archive. A restore never leaves archive credentials live."""
    calls = []

    def fake_contents(archive_path, backup_root, expected_sha256=None, data_key=None, **kwargs):
        calls.append(str(archive_path))
        with factory.begin() as db:
            db.merge(SystemSetting(
                key="restore_marker",
                value="archive" if len(calls) == 1 else "pre-restore",
                updated_by="test",
            ))

    def boom(**kwargs):
        raise RuntimeError("injected finalize failure")

    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_contents)
    monkeypatch.setattr(backup_workflow, "_finalize_restored_security", boom)
    archive = _patch_restore_scaffolding(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="previous database was restored"):
        backup_workflow.restore_backup(archive, "ab" * 32, actor="guard-admin", reason="اختبار")

    assert calls == [str(archive), str(tmp_path / "pre.zip")], \
        "rollback must restore the pre-restore archive after a finalize failure"
    with factory() as db:
        assert db.get(SystemSetting, "restore_marker").value == "pre-restore"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "backup_restore_rolled_back")) is not None


def _login(client, username="guard-admin", password=ADMIN_PW):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_activate_refused_for_restore_locked_account(factory):
    """A restore-locked account must not come back through plain
    re-activation: its archive-time password hash is still on the row, so the
    old secret would sign in. Only the audited reset flow may re-enable it."""
    with factory.begin() as db:
        db.add(AdminAccount(username="locked-ghost", password_hash=hash_password("Ghost-Old-Pass-111!"),
                            display_name="شبح النسخة", role="finance", active=False,
                            must_change_password=True, pending_credential_reset=True))

    with TestClient(app) as client:
        _login(client)
        page = client.get("/settings/accounts")
        assert "رجع من نسخة قديمة" in page.text, "the row must explain the reset-only path"
        with factory() as db:
            ghost_id = db.scalar(select(AdminAccount.id).where(AdminAccount.username == "locked-ghost"))
        response = client.post(
            f"/settings/accounts/{ghost_id}/activate",
            data={"csrf_token": _csrf(page), "reason": "تجربة"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/accounts?error=reset_required"

    with factory() as db:
        ghost = db.scalar(select(AdminAccount).where(AdminAccount.username == "locked-ghost"))
        assert ghost.active is False and ghost.pending_credential_reset is True


def test_reset_password_reenables_restore_locked_account(factory):
    """The owner-approved re-enable: the audited reset flow destroys the old
    archive hash, activates the account, clears the flag, closes its sessions
    and leaves must_change_password on for the owner's own fresh password."""
    from app.security import verify_password

    with factory.begin() as db:
        db.add(AdminAccount(username="locked-ghost", password_hash=hash_password("Ghost-Old-Pass-111!"),
                            display_name="شبح النسخة", role="finance", active=False,
                            must_change_password=True, pending_credential_reset=True))

    with TestClient(app) as client:
        _login(client)
        page = client.get("/settings/accounts")
        token = _csrf(page)
        with factory() as db:
            ghost_id = db.scalar(select(AdminAccount.id).where(AdminAccount.username == "locked-ghost"))
        response = client.post(
            f"/settings/accounts/{ghost_id}/reset-password",
            data={"csrf_token": token, "reason": "إعادة تفعيل بعد استرجاع", "current_password": ADMIN_PW},
        )
        assert response.status_code == 200, "reset renders the one-time reveal page"
        match = re.search(r"<bdi><code>([^<]+)</code></bdi>", response.text)
        assert match, "a new one-time password must be revealed once"
        temporary_password = match.group(1)
        assert "أُعيد تفعيله الآن بكلمة المرور الجديدة" in response.text

    with factory() as db:
        ghost = db.scalar(select(AdminAccount).where(AdminAccount.username == "locked-ghost"))
        assert ghost.active is True, "reset re-enables the account"
        assert ghost.pending_credential_reset is False
        assert ghost.must_change_password is True, "owner still sets a personal password at first sign-in"
        assert verify_password(temporary_password, ghost.password_hash)
        assert not verify_password("Ghost-Old-Pass-111!", ghost.password_hash), "old archive secret is destroyed"
        actions = [a.action for a in db.scalars(select(AuditLog)).all()]
        assert "account_password_reset" in actions
        assert "account_reenabled_with_reset" in actions

    with TestClient(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "locked-ghost", "password": "Ghost-Old-Pass-111!", "csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 401, "the old archive password must never sign in again"
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"username": "locked-ghost", "password": temporary_password, "csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303, "the new one-time password signs in"


def test_telegram_setting_carried_through_capture_reapply(factory):
    """The telegram binding is credential-class state: capture/reapply keeps
    the CURRENT destination and nonce state so a restore cannot revert the
    alert chat, revive an old nonce, or lose a concurrent bind."""
    import json

    from app.models import SystemSetting

    current = json.dumps({"bot_token": "tok", "chat_id": "555",
                          "bind_nonce": "", "bind_nonce_used": True,
                          "bind_nonce_expires_at": ""}, ensure_ascii=False)
    with factory.begin() as db:
        db.merge(SystemSetting(key="telegram_alerts", value=current, updated_by="test"))

    with factory() as db:
        snapshot = backup_workflow._capture_security_state(db)
    assert snapshot["settings"]["telegram_alerts"] == current

    with factory.begin() as db:
        # Simulate the archive's older binding having replaced the row.
        stale = json.dumps({"bot_token": "tok", "chat_id": "111",
                            "bind_nonce": "stale-nonce", "bind_nonce_used": False,
                            "bind_nonce_expires_at": "2026-01-01"}, ensure_ascii=False)
        db.merge(SystemSetting(key="telegram_alerts", value=stale, updated_by="restore"))
        backup_workflow._reapply_security_state(db, snapshot, actor="test")

    with factory() as db:
        setting = db.get(SystemSetting, "telegram_alerts")
        assert json.loads(setting.value)["chat_id"] == "555"
        assert json.loads(setting.value)["bind_nonce"] == ""


def test_neutralize_revived_bind_nonce(factory):
    """An archive-revived one-time bind nonce is cleared (no stale deep link
    can bind a new chat); an absent setting and an empty nonce are no-ops."""
    import json

    from app.models import SystemSetting

    live = json.dumps({"bot_token": "tok", "chat_id": "",
                       "bind_nonce": "revived-nonce", "bind_nonce_used": False,
                       "bind_nonce_expires_at": "2026-12-01"}, ensure_ascii=False)
    with factory.begin() as db:
        db.merge(SystemSetting(key="telegram_alerts", value=live, updated_by="restore"))
    with factory.begin() as db:
        backup_workflow._neutralize_revived_bind_nonce(db, actor="test")
    with factory() as db:
        record = json.loads(db.get(SystemSetting, "telegram_alerts").value)
        assert record["bind_nonce"] == ""
        assert record["bind_nonce_used"] is True
        assert record["bind_nonce_expires_at"] == ""

    with factory.begin() as db:
        db.merge(SystemSetting(key="telegram_alerts",
                               value=json.dumps({"chat_id": "9"}), updated_by="test"))
    with factory.begin() as db:
        backup_workflow._neutralize_revived_bind_nonce(db, actor="test")
    with factory() as db:
        assert json.loads(db.get(SystemSetting, "telegram_alerts").value)["chat_id"] == "9"
