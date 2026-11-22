"""Backup encryption: passphrase wrapping, chunked AES-GCM, restore integration."""
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import backup_crypto
from app.backup_crypto import (
    PassphraseRequiredError,
    WrongPassphraseError,
    decrypt_file,
    encrypt_file,
    has_passphrase,
    is_encrypted_archive,
    load_data_key,
    set_passphrase,
    unwrap_data_key_for_restore,
    verify_passphrase,
)
from app.backup_workflow import cleanup_retention, verify_backup_archive
from app.db import Base

PASSPHRASE = "correct horse battery staple 123"


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_set_and_verify_passphrase(db_factory):
    with db_factory.begin() as db:
        assert not has_passphrase(db)
        set_passphrase(db, PASSPHRASE, "admin")
        assert has_passphrase(db)
        assert verify_passphrase(db, PASSPHRASE)
        assert not verify_passphrase(db, "wrong passphrase")
        assert load_data_key(db) is not None
        with pytest.raises(WrongPassphraseError):
            unwrap_data_key_for_restore(db, "wrong passphrase")


def test_passphrase_too_short_and_change_rules(db_factory):
    with db_factory.begin() as db:
        with pytest.raises(ValueError, match="passphrase_too_short"):
            set_passphrase(db, "short", "admin")
        set_passphrase(db, PASSPHRASE, "admin")
        with pytest.raises(ValueError, match="current_passphrase_required"):
            set_passphrase(db, "a brand new passphrase", "admin")
        with pytest.raises(WrongPassphraseError):
            set_passphrase(db, "a brand new passphrase", "admin", current_passphrase="wrong one")


def test_change_passphrase_keeps_backups_readable(db_factory, tmp_path):
    with db_factory.begin() as db:
        set_passphrase(db, PASSPHRASE, "admin")
        key_before = load_data_key(db)
        payload = tmp_path / "plain.bin"
        payload.write_bytes(b"synthetic backup bytes" * 100)
        enc = tmp_path / "archive.zip.enc"
        encrypt_file(key_before, payload, enc)
        set_passphrase(db, "new passphrase 456", "admin", current_passphrase=PASSPHRASE)
        assert not verify_passphrase(db, PASSPHRASE)
        unwrapped = unwrap_data_key_for_restore(db, "new passphrase 456")
        assert unwrapped == key_before
        out = tmp_path / "out.bin"
        decrypt_file(unwrapped, enc, out)
        assert out.read_bytes() == payload.read_bytes()


def test_chunked_roundtrip_and_tamper_detection(db_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_crypto, "CHUNK_SIZE", 1024)
    with db_factory.begin() as db:
        set_passphrase(db, PASSPHRASE, "admin")
        key = load_data_key(db)
    payload = tmp_path / "big.bin"
    payload.write_bytes(bytes(range(256)) * 40)  # ~10 KB across many 1 KB chunks
    enc = tmp_path / "big.zip.enc"
    encrypt_file(key, payload, enc)
    assert is_encrypted_archive(enc)
    assert not is_encrypted_archive(payload)
    out = tmp_path / "big.out"
    decrypt_file(key, enc, out)
    assert out.read_bytes() == payload.read_bytes()
    # Wrong key fails.
    with pytest.raises(ValueError):
        decrypt_file(b"\x01" * 32, enc, tmp_path / "wrong.out")
    # Truncation fails (end marker missing).
    truncated = tmp_path / "truncated.enc"
    truncated.write_bytes(enc.read_bytes()[:-8])
    with pytest.raises(ValueError):
        decrypt_file(key, truncated, tmp_path / "trunc.out")
    # Dropped middle chunk fails integrity.
    data = bytearray(enc.read_bytes())
    header = 8 + 8 + 4
    block = 1024 + 16
    del data[header + block:header + 2 * block]
    damaged = tmp_path / "damaged.enc"
    damaged.write_bytes(bytes(data))
    with pytest.raises(ValueError):
        decrypt_file(key, damaged, tmp_path / "damaged.out")


def test_key_file_enables_disaster_recovery_without_database(db_factory, tmp_path):
    from app.backup_crypto import export_public_record, unwrap_data_key_from_record
    from app.backup_workflow import KEY_FILE_NAME, export_key_file
    from app.models import SystemSetting

    with db_factory.begin() as db:
        db.add(SystemSetting(key="backup_root", value=str(tmp_path), updated_by="admin"))
        set_passphrase(db, PASSPHRASE, "admin")
        expected_key = load_data_key(db)
        key_path = export_key_file(db)
    assert key_path == tmp_path / KEY_FILE_NAME
    payload = json.loads(key_path.read_text(encoding="utf-8"))
    assert "data_key" not in payload  # the key file never carries the plaintext key
    assert payload["wrapped_key"] and payload["kdf_salt"]
    # Fresh-install recovery: unwrap straight from the exported record.
    assert unwrap_data_key_from_record(payload, PASSPHRASE) == expected_key
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key_from_record(payload, "wrong passphrase")


def _make_plain_zip(path: Path, backup_type: str = "daily", created: datetime | None = None) -> Path:
    created = created or datetime(2026, 11, 20, tzinfo=timezone.utc)
    dump = b"synthetic PostgreSQL custom-format dump bytes"
    manifest = {
        "format_version": 1,
        "application": "M.Y. Business Hotel HR & Payroll System",
        "backup_type": backup_type,
        "created_at": created.isoformat(),
        "database_sha256": hashlib.sha256(dump).hexdigest(),
        "employee_photos_sha256": hashlib.sha256(b"").hexdigest(),
        "employee_photo_count": 0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("database.dump", dump)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_verify_encrypted_archive_needs_data_key(db_factory, tmp_path):
    with db_factory.begin() as db:
        set_passphrase(db, PASSPHRASE, "admin")
        key = load_data_key(db)
    plain = _make_plain_zip(tmp_path / "daily" / "valid.zip")
    enc = tmp_path / "daily" / "valid.zip.enc"
    encrypt_file(key, plain, enc)
    plain.unlink()
    with pytest.raises(PassphraseRequiredError):
        verify_backup_archive(enc)
    manifest = verify_backup_archive(enc, data_key=key)
    assert manifest["backup_type"] == "daily"


def test_retention_handles_encrypted_archives(db_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_crypto, "CHUNK_SIZE", 1024)
    with db_factory.begin() as db:
        set_passphrase(db, PASSPHRASE, "admin")
        key = load_data_key(db)
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    recent = datetime(2026, 11, 20, tzinfo=timezone.utc)
    for name, stamp in (("old", old), ("recent", recent)):
        plain = _make_plain_zip(tmp_path / "daily" / f"{name}.zip", "daily", stamp)
        enc = tmp_path / "daily" / f"{name}.zip.enc"
        encrypt_file(key, plain, enc)
        plain.unlink()
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    # Without the key nothing is auto-deleted.
    assert cleanup_retention(tmp_path, now) == []
    removed = cleanup_retention(tmp_path, now, data_key=key)
    assert len(removed) == 1 and "old" in removed[0]
    assert (tmp_path / "daily" / "recent.zip.enc").is_file()
    assert not (tmp_path / "daily" / "old.zip.enc").exists()


def test_damaged_record_fails_closed(db_factory, tmp_path, monkeypatch):
    """A damaged encryption record must never silently yield plaintext archives."""
    from app import backup_workflow
    from app.backup_crypto import SETTING_KEY, EncryptionRecordError
    from app.models import SystemSetting

    with db_factory.begin() as db:
        set_passphrase(db, PASSPHRASE, "admin")
        record = db.get(SystemSetting, SETTING_KEY)
        assert has_passphrase(db)

        record.value = "{not-json"
        db.flush()
        assert has_passphrase(db)  # presence, not parseability
        with pytest.raises(EncryptionRecordError):
            load_data_key(db)

        record.value = json.dumps({"version": 2})
        db.flush()
        with pytest.raises(EncryptionRecordError):
            load_data_key(db)

        record.value = json.dumps({"version": 1, "data_key": "not-hex"})
        db.flush()
        with pytest.raises(EncryptionRecordError):
            load_data_key(db)

        # create_backup fails closed: failed run recorded, no archive written.
        root = tmp_path / "backups"
        root.mkdir()
        db.add(SystemSetting(key=backup_workflow.BACKUP_SETTING_KEY, value=str(root), updated_by="synthetic-admin"))
        db.flush()
        monkeypatch.setattr(
            backup_workflow,
            "validate_backup_root",
            lambda path_value, require_external=True: Path(path_value),
        )
        run = backup_workflow.create_backup(db, "manual", "synthetic-admin")
        assert run.status == "failed"
        assert "damaged" in run.error_message
        assert list(root.iterdir()) == []

        # verify_passphrase fails closed on the damaged record too.
        with pytest.raises(ValueError):
            verify_passphrase(db, PASSPHRASE)


def test_restore_cli_rejects_missing_archive(tmp_path):
    from app.restore_cli import main

    assert main(["--archive", str(tmp_path / "missing.zip.enc")]) == 2


def test_restore_cli_language_selection(tmp_path, capsys, monkeypatch):
    from app.restore_cli import main

    missing = tmp_path / "missing.zip.enc"
    assert main(["--archive", str(missing)]) == 2
    assert "النسخة غير موجودة" in capsys.readouterr().out  # Arabic default
    assert main(["--archive", str(missing), "--lang", "zh"]) == 2
    assert "找不到备份文件" in capsys.readouterr().out
    monkeypatch.setenv("MYHOTEL_LANG", "en")
    assert main(["--archive", str(missing)]) == 2
    assert "Archive not found" in capsys.readouterr().out
    monkeypatch.setenv("MYHOTEL_LANG", "fr")
    assert main(["--archive", str(missing)]) == 2
    assert "Unsupported language" in capsys.readouterr().out
