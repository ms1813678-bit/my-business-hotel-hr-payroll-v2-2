"""CLI disaster-restore entry: key-file fallback and preserved-record matching."""
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import backup_workflow
from app.db import Base
from app.models import BackupRun


def _make_backup(path: Path, backup_type: str, created_at: datetime) -> Path:
    database_dump = b"synthetic PostgreSQL custom-format dump bytes"
    manifest = {
        "format_version": 1,
        "application": "M.Y. Business Hotel HR & Payroll System",
        "backup_type": backup_type,
        "created_at": created_at.isoformat(),
        "database_sha256": hashlib.sha256(database_dump).hexdigest(),
        "employee_photos_sha256": hashlib.sha256(b"").hexdigest(),
        "employee_photo_count": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("database.dump", database_dump)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_restore_cli_uses_preserved_record_when_canonical_overwritten(tmp_path, monkeypatch):
    """Fresh install B (own passphrase) restores install A's archive through
    the CLI: the racer-overwritten canonical record fails, the preserved
    record in the store opens the archive."""
    import getpass

    from app import restore_cli
    from app.backup_crypto import encrypt_file, export_public_record, load_data_key, set_passphrase
    from app.backup_workflow import sha256_file
    from app.models import SystemSetting

    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    # Install A's record + encrypted archive.
    src_engine = create_engine("sqlite://")
    Base.metadata.create_all(src_engine)
    with Session(src_engine) as src_db:
        set_passphrase(src_db, "synthetic-passphrase-alice", actor="test")
        src_db.commit()
        record_a = export_public_record(src_db)
        key_a = load_data_key(src_db)
    src_engine.dispose()
    plain = _make_backup(tmp_path / "plain.zip", "manual", datetime(2026, 11, 7, tzinfo=timezone.utc))
    archive = backup_root / "manual" / "archived.zip.enc"
    archive.parent.mkdir(parents=True, exist_ok=True)
    encrypt_file(key_a, plain, archive)

    # Shared drive: a racer overwrote the canonical record; A's is preserved.
    (backup_root / "backup_encryption_key.json").write_text(json.dumps({
        "version": 1, "wrapped_key": "ff" * 64, "kdf_salt": "00" * 16, "wrap_nonce": "00" * 12,
    }), encoding="utf-8")
    digest = hashlib.sha256(record_a["wrapped_key"].encode("utf-8")).hexdigest()[:8]
    (backup_root / f"backup_encryption_key.prev-{digest}.json").write_text(
        json.dumps(record_a), encoding="utf-8"
    )

    # Fresh install B: own passphrase (CLI gate) + configured root.
    engine = create_engine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        set_passphrase(db, "synthetic-passphrase-bob", actor="cli-admin")
        db.add(SystemSetting(key="backup_root", value=str(backup_root), updated_by="cli-admin"))

    snap = _make_backup(
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

    monkeypatch.setattr(restore_cli, "SessionLocal", factory)
    monkeypatch.setattr(restore_cli, "_apply_migrations", lambda: None)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(backup_workflow, "create_backup", fake_create_backup)
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_restore)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "synthetic-passphrase-alice")
    monkeypatch.setattr("builtins.input", lambda prompt="": "RESTORE")

    rc = restore_cli.main(["--archive", str(archive), "--actor", "synthetic-cli"])
    assert rc == 0
    assert captured[0] == (archive.name, key_a)
    engine.dispose()


def test_restore_cli_wrong_passphrase_fails_before_restore(tmp_path, monkeypatch, capsys):
    """Wrong passphrase at the CLI: nonzero exit, no snapshot, no restore."""
    import getpass

    from app import restore_cli
    from app.backup_crypto import encrypt_file, export_public_record, load_data_key, set_passphrase
    from app.models import SystemSetting

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    src_engine = create_engine("sqlite://")
    Base.metadata.create_all(src_engine)
    with Session(src_engine) as src_db:
        set_passphrase(src_db, "synthetic-passphrase-alice", actor="test")
        src_db.commit()
        record_a = export_public_record(src_db)
        key_a = load_data_key(src_db)
    src_engine.dispose()
    plain = _make_backup(tmp_path / "plain.zip", "manual", datetime(2026, 11, 7, tzinfo=timezone.utc))
    archive = backup_root / "manual" / "archived.zip.enc"
    archive.parent.mkdir(parents=True, exist_ok=True)
    encrypt_file(key_a, plain, archive)
    (backup_root / "backup_encryption_key.json").write_text(json.dumps(record_a), encoding="utf-8")

    engine = create_engine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        set_passphrase(db, "synthetic-passphrase-bob", actor="cli-admin")
        db.add(SystemSetting(key="backup_root", value=str(backup_root), updated_by="cli-admin"))

    called = []

    def should_not_run(*args, **kwargs):
        called.append(True)
        raise AssertionError("restore must not start with a wrong passphrase")

    monkeypatch.setattr(restore_cli, "SessionLocal", factory)
    monkeypatch.setattr(restore_cli, "_apply_migrations", lambda: None)
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", should_not_run)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "synthetic-passphrase-wrong")
    monkeypatch.setattr("builtins.input", lambda prompt="": "RESTORE")

    rc = restore_cli.main(["--archive", str(archive), "--actor", "synthetic-cli"])
    assert rc != 0
    assert called == []
    engine.dispose()
