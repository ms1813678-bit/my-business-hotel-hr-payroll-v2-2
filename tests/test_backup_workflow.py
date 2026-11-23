"""Backup archive integrity tests. Backups are never deleted automatically."""
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.backup_workflow import (
    create_backup,
    restore_backup,
    sha256_file,
    validate_backup_root,
    verify_backup_archive,
)
from app.db import Base
from app.models import BackupRun


def make_backup(path: Path, backup_type: str, created_at: datetime) -> Path:
    database_dump = b"synthetic PostgreSQL custom-format dump bytes"
    photo = b"synthetic employee photo bytes"
    photo_hash = hashlib.sha256()
    photo_hash.update(b"employee.jpg")
    photo_hash.update(hashlib.sha256(photo).hexdigest().encode("ascii"))
    manifest = {
        "format_version": 1,
        "application": "M.Y. Business Hotel HR & Payroll System",
        "backup_type": backup_type,
        "created_at": created_at.isoformat(),
        "database_sha256": hashlib.sha256(database_dump).hexdigest(),
        "employee_photos_sha256": photo_hash.hexdigest(),
        "employee_photo_count": 1,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("database.dump", database_dump)
        archive.writestr("employee_photos/employee.jpg", photo)
        archive.writestr("manifest.json", json.dumps(manifest))
    return path


def test_backup_archive_hash_and_photo_manifest_are_verified(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    archive = make_backup(tmp_path / "daily" / "valid.zip", "daily", now)
    manifest = verify_backup_archive(archive, sha256_file(archive))
    assert manifest["backup_type"] == "daily"
    assert manifest["employee_photo_count"] == 1

    with pytest.raises(ValueError, match="hash"):
        verify_backup_archive(archive, "0" * 64)


def test_backup_creation_performs_no_automatic_retention_deletion():
    # Owner decision (November 2026): no backup of any type or age is ever
    # deleted automatically; the user deletes old archives manually.
    import inspect

    from app import backup_workflow

    assert not hasattr(backup_workflow, "cleanup_retention")
    assert "cleanup_retention" not in inspect.getsource(backup_workflow.create_backup)


def test_backup_location_requires_an_absolute_path(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        validate_backup_root("relative/backups", require_external=False)
    valid = validate_backup_root(str(tmp_path / "external-backups"), require_external=False)
    assert valid.is_absolute()
    assert valid.is_dir()


def test_backup_without_configured_external_drive_is_recorded_as_failed(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            run = create_backup(db, "manual", "synthetic-admin")
            assert run.status == "failed"
            assert "external backup drive" in run.error_message
            db.commit()
            saved = db.scalar(select(BackupRun))
            assert saved.status == "failed"
            assert saved.archive_path is None
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_backup_archive_rejects_unsafe_duplicate_and_unexpected_members(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    archive = tmp_path / "manual" / "unsafe.zip"
    archive.parent.mkdir(parents=True)
    manifest = {
        "format_version": 1,
        "application": "M.Y. Business Hotel HR & Payroll System",
        "backup_type": "manual",
        "created_at": now.isoformat(),
        "database_sha256": hashlib.sha256(b"dump").hexdigest(),
        "employee_photos_sha256": hashlib.sha256(b"").hexdigest(),
        "employee_photo_count": 0,
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("database.dump", b"dump")
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("../outside.txt", b"unsafe")
    with pytest.raises(ValueError, match="unsafe"):
        verify_backup_archive(archive)


def test_backup_archive_rejects_unknown_application(tmp_path):
    now = datetime(2026, 11, 7, tzinfo=timezone.utc)
    archive = tmp_path / "manual" / "foreign.zip"
    archive.parent.mkdir(parents=True)
    dump = b"dump"
    manifest = {
        "format_version": 1,
        "application": "Other Application",
        "backup_type": "manual",
        "created_at": now.isoformat(),
        "database_sha256": hashlib.sha256(dump).hexdigest(),
        "employee_photos_sha256": hashlib.sha256(b"").hexdigest(),
        "employee_photo_count": 0,
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("database.dump", dump)
        zf.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="unknown application"):
        verify_backup_archive(archive)


def test_restore_backup_rejects_empty_reason_before_any_restore(monkeypatch, tmp_path):
    archive = tmp_path / "manual" / "valid.zip"
    make_backup(archive, "manual", datetime(2026, 11, 7, tzinfo=timezone.utc))

    called = {"value": False}

    def should_not_run(*args, **kwargs):
        called["value"] = True
        raise AssertionError("restore must not start when reason is empty")

    monkeypatch.setattr("app.backup_workflow._restore_archive_contents", should_not_run)

    with pytest.raises(ValueError, match="restore reason"):
        restore_backup(
            archive,
            sha256_file(archive),
            actor="synthetic-admin",
            reason="   ",
        )

    assert called["value"] is False


def _prepare_restore_test_environment(monkeypatch, tmp_path, *, fail_selected_restore=False):
    from sqlalchemy.orm import sessionmaker

    import app.backup_workflow as backup_workflow
    import app.db as db_module
    from app.models import AuditLog, SystemSetting

    database_path = tmp_path / "restore-test.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    backup_root = tmp_path / "external-backups"
    selected_archive = make_backup(
        backup_root / "manual" / "selected.zip",
        "manual",
        datetime(2026, 11, 7, tzinfo=timezone.utc),
    )
    pre_restore_archive = make_backup(
        backup_root / "pre_restore" / "pre-restore.zip",
        "pre_restore",
        datetime(2026, 11, 8, tzinfo=timezone.utc),
    )
    pre_restore_hash = sha256_file(pre_restore_archive)
    selected_hash = sha256_file(selected_archive)

    with factory.begin() as db:
        db.add(SystemSetting(
            key="backup_root",
            value=str(backup_root),
            updated_by="synthetic-admin",
        ))

    def record_pre_restore_backup(db, backup_type, actor):
        assert backup_type == "pre_restore"
        run = BackupRun(
            backup_type="pre_restore",
            status="success",
            archive_path=str(pre_restore_archive),
            sha256=pre_restore_hash,
            size_bytes=pre_restore_archive.stat().st_size,
            created_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
            verified_at=datetime(2026, 11, 8, tzinfo=timezone.utc),
            initiated_by=actor,
        )
        db.add(run)
        db.flush()
        return run

    restore_calls = []

    def restore_archive(path, root, expected_sha256=None, data_key=None, **kwargs):
        path = Path(path)
        restore_calls.append(path)
        if fail_selected_restore and path == selected_archive:
            raise RuntimeError("synthetic selected restore failure")
        assert root == backup_root
        if path == pre_restore_archive:
            assert expected_sha256 == pre_restore_hash

    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(backup_workflow, "create_backup", record_pre_restore_backup)
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", restore_archive)

    return {
        "engine": engine,
        "factory": factory,
        "backup_root": backup_root,
        "selected_archive": selected_archive,
        "selected_hash": selected_hash,
        "pre_restore_archive": pre_restore_archive,
        "restore_calls": restore_calls,
        "AuditLog": AuditLog,
    }


def test_restore_backup_records_audit_and_history_after_success(monkeypatch, tmp_path):
    context = _prepare_restore_test_environment(monkeypatch, tmp_path)
    try:
        result = restore_backup(
            context["selected_archive"],
            context["selected_hash"],
            actor="synthetic-admin",
            reason="Synthetic successful restore test",
        )

        assert context["restore_calls"] == [context["selected_archive"]]
        assert result["archive_path"] == str(context["selected_archive"])
        assert result["pre_restore_archive"] == str(context["pre_restore_archive"])
        history = context["backup_root"] / "restore_history.jsonl"
        assert history.is_file()
        assert '"reason": "Synthetic successful restore test"' in history.read_text(encoding="utf-8")

        with context["factory"]() as db:
            actions = db.scalars(select(context["AuditLog"].action)).all()
            pre_restore_runs = db.scalars(
                select(BackupRun).where(BackupRun.backup_type == "pre_restore")
            ).all()
        assert actions == ["backup_restored"]
        assert len(pre_restore_runs) == 1
    finally:
        context["engine"].dispose()


def test_restore_backup_rolls_back_after_selected_archive_failure(monkeypatch, tmp_path):
    context = _prepare_restore_test_environment(
        monkeypatch, tmp_path, fail_selected_restore=True
    )
    try:
        with pytest.raises(RuntimeError, match="previous database was restored"):
            restore_backup(
                context["selected_archive"],
                context["selected_hash"],
                actor="synthetic-admin",
                reason="Synthetic rollback test",
            )

        assert context["restore_calls"] == [
            context["selected_archive"],
            context["pre_restore_archive"],
        ]
        assert not (context["backup_root"] / "restore_history.jsonl").exists()

        with context["factory"]() as db:
            actions = db.scalars(select(context["AuditLog"].action)).all()
            pre_restore_runs = db.scalars(
                select(BackupRun).where(BackupRun.backup_type == "pre_restore")
            ).all()
        assert actions == ["backup_restore_rolled_back"]
        assert len(pre_restore_runs) == 1
        assert pre_restore_runs[0].archive_path == str(context["pre_restore_archive"])
    finally:
        context["engine"].dispose()



def test_every_backup_contains_the_admin_accounts():
    # Owner decision (2026-11-23, item 84): the system administrator's account
    # must appear in every backup. create_backup dumps the WHOLE database
    # (pg_dump custom format, no table/schema/data filtering), so the admin
    # accounts table is structurally inside every archive; verified live on
    # PostgreSQL 16.2 (archive TABLE DATA for admin_accounts, admin row
    # included). Keep it that way: current-account restore compatibility
    # depends on it.
    import inspect

    from app import backup_workflow

    source = inspect.getsource(backup_workflow.create_backup)
    assert "--format=custom" in source
    assert "--exclude-table" not in source
    assert "--table" not in source
    assert "--schema" not in source
    assert "--data-only" not in source


def test_backup_without_configured_passphrase_is_recorded_as_failed(tmp_path, monkeypatch):
    from app import backup_workflow
    from app.models import SystemSetting

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            root = tmp_path / "backups"
            root.mkdir()
            db.add(SystemSetting(
                key=backup_workflow.BACKUP_SETTING_KEY,
                value=str(root),
                updated_by="synthetic-admin",
            ))
            db.flush()
            monkeypatch.setattr(
                backup_workflow,
                "validate_backup_root",
                lambda path_value, require_external=True: Path(path_value),
            )
            run = create_backup(db, "manual", "synthetic-admin")
            assert run.status == "failed"
            assert "passphrase" in run.error_message
            assert list(root.iterdir()) == []
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_rollback_uses_install_data_key_not_archive_key(tmp_path, monkeypatch):
    import json as jsonlib

    from sqlalchemy.orm import sessionmaker

    from app import backup_workflow
    from app.backup_crypto import (
        encrypt_file,
        export_public_record,
        load_data_key,
        set_passphrase,
    )
    from app.models import SystemSetting

    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    # Source install: passphrase B -> encrypted archive + exported key record.
    src_engine = create_engine("sqlite://")
    Base.metadata.create_all(src_engine)
    with Session(src_engine) as src_db:
        set_passphrase(src_db, "synthetic-source-passphrase-b", actor="test")
        src_db.commit()
        archive_key = load_data_key(src_db)
        public_record = export_public_record(src_db)
    src_engine.dispose()
    plain = tmp_path / "plain.zip"
    make_backup(plain, "manual", datetime(2026, 11, 7, tzinfo=timezone.utc))
    archive = backup_root / "manual" / "archived.zip.enc"
    archive.parent.mkdir(parents=True)
    encrypt_file(archive_key, plain, archive)
    (backup_root / "backup_encryption_key.json").write_text(
        jsonlib.dumps(public_record), encoding="utf-8"
    )

    # Target install: passphrase A with a DIFFERENT data key (fresh install).
    tgt_engine = create_engine("sqlite://")
    Base.metadata.create_all(tgt_engine)
    factory = sessionmaker(bind=tgt_engine)
    with factory.begin() as db:
        set_passphrase(db, "synthetic-target-passphrase-a", actor="test")
        db.add(SystemSetting(
            key=backup_workflow.BACKUP_SETTING_KEY,
            value=str(backup_root),
            updated_by="synthetic-admin",
        ))
    with factory() as db:
        target_key = load_data_key(db)
    assert target_key != archive_key
    monkeypatch.setattr("app.db.SessionLocal", factory)
    monkeypatch.setattr(
        backup_workflow,
        "validate_backup_root",
        lambda path_value, require_external=True: Path(path_value),
    )

    # The pre-restore snapshot is encrypted with the TARGET install's key.
    snap_plain = tmp_path / "snap.zip"
    make_backup(snap_plain, "pre_restore", datetime(2026, 11, 8, tzinfo=timezone.utc))
    snap = backup_root / "pre_restore" / "snap.zip.enc"
    snap.parent.mkdir(parents=True)
    encrypt_file(target_key, snap_plain, snap)

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

    monkeypatch.setattr(backup_workflow, "create_backup", fake_create_backup)

    seen = []

    def fake_restore(archive_path, root, sha, data_key, **kwargs):
        seen.append((Path(archive_path).name, data_key))
        if len(seen) == 1:
            raise RuntimeError("injected mid-restore failure")

    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_restore)

    with pytest.raises(RuntimeError, match="previous database was restored"):
        backup_workflow.restore_backup(
            archive,
            sha256_file(archive),
            actor="synthetic-admin",
            reason="synthetic rollback key test",
            passphrase="synthetic-source-passphrase-b",
        )

    assert seen[0] == (archive.name, archive_key)  # selected archive opens with its own key
    assert seen[1] == (snap.name, target_key)  # rollback uses the INSTALL's key, not the archive's
    tgt_engine.dispose()


def _standalone_install_record(passphrase: str):
    """A public key record and data key from a synthetic standalone install."""
    from app.backup_crypto import export_public_record, load_data_key, set_passphrase

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        set_passphrase(db, passphrase, actor="test")
        db.commit()
        record = export_public_record(db)
        key = load_data_key(db)
    engine.dispose()
    return record, key


def _key_export_session(backup_root: Path, passphrase: str):
    from sqlalchemy.orm import sessionmaker

    from app.backup_crypto import set_passphrase
    from app.models import SystemSetting

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory.begin() as db:
        set_passphrase(db, passphrase, actor="test")
        db.add(SystemSetting(key="backup_root", value=str(backup_root), updated_by="test"))
    return engine, factory


def test_export_key_file_preserves_replaced_record(tmp_path):
    import json as jsonlib

    from app.backup_crypto import export_public_record
    from app.backup_workflow import KEY_FILE_NAME, export_key_file

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    canonical = backup_root / KEY_FILE_NAME

    engine_a, factory_a = _key_export_session(backup_root, "synthetic-passphrase-alice")
    with factory_a() as db:
        record_a = export_public_record(db)
        export_key_file(db)
    engine_a.dispose()
    assert jsonlib.loads(canonical.read_text(encoding="utf-8"))["wrapped_key"] == record_a["wrapped_key"]

    engine_b, factory_b = _key_export_session(backup_root, "synthetic-passphrase-bob")
    with factory_b() as db:
        record_b = export_public_record(db)
        export_key_file(db)
    engine_b.dispose()

    # B's record is canonical now; A's record survives in the record store.
    assert jsonlib.loads(canonical.read_text(encoding="utf-8"))["wrapped_key"] == record_b["wrapped_key"]
    digest = hashlib.sha256(record_a["wrapped_key"].encode("utf-8")).hexdigest()[:8]
    preserved = backup_root / f"backup_encryption_key.prev-{digest}.json"
    assert preserved.is_file()
    assert jsonlib.loads(preserved.read_text(encoding="utf-8")) == record_a


def test_export_key_file_record_store_survives_canonical_lost_update(tmp_path):
    """A racing export that overwrites the canonical file without preserving
    still cannot strand another installation's record: every export leaves a
    content-addressed copy of its own record first."""
    import json as jsonlib

    from app.backup_crypto import export_public_record
    from app.backup_workflow import KEY_FILE_NAME, _recovery_key_records, export_key_file

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    canonical = backup_root / KEY_FILE_NAME

    engine_a, factory_a = _key_export_session(backup_root, "synthetic-passphrase-alice")
    with factory_a() as db:
        record_a = export_public_record(db)
        export_key_file(db)
    engine_a.dispose()
    engine_b, factory_b = _key_export_session(backup_root, "synthetic-passphrase-bob")
    with factory_b() as db:
        record_b = export_public_record(db)
        export_key_file(db)
    engine_b.dispose()

    # Simulated lost update: a third install overwrites the canonical file
    # with its own record WITHOUT preserving (the old behavior / a race).
    record_c, _ = _standalone_install_record("synthetic-passphrase-carol")
    canonical.write_text(jsonlib.dumps(record_c), encoding="utf-8")

    records, saw_file, saw_unreadable = _recovery_key_records(backup_root)
    recovered = {record["wrapped_key"] for record in records}
    assert record_a["wrapped_key"] in recovered
    assert record_b["wrapped_key"] in recovered
    assert record_c["wrapped_key"] in recovered
    assert saw_file and not saw_unreadable


def test_export_key_file_digest_collision_uses_longer_name(tmp_path):
    """A truncated-digest filename collision with a DIFFERENT record must not
    silently skip the write: the store lengthens the name instead."""
    import json as jsonlib

    from app.backup_crypto import export_public_record
    from app.backup_workflow import PRESERVED_KEY_FILE_PREFIX, export_key_file

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    engine, factory = _key_export_session(backup_root, "synthetic-passphrase-alice")
    with factory() as db:
        record = export_public_record(db)
        # Squat on the 8-char name with a different record's content.
        short = hashlib.sha256(record["wrapped_key"].encode("utf-8")).hexdigest()[:8]
        other, _ = _standalone_install_record("synthetic-passphrase-bob")
        squatter = backup_root / f"{PRESERVED_KEY_FILE_PREFIX}{short}.json"
        squatter.write_text(jsonlib.dumps(other), encoding="utf-8")
        export_key_file(db)
    engine.dispose()
    longer = hashlib.sha256(record["wrapped_key"].encode("utf-8")).hexdigest()[:16]
    stored = backup_root / f"{PRESERVED_KEY_FILE_PREFIX}{longer}.json"
    assert stored.is_file()
    assert jsonlib.loads(stored.read_text(encoding="utf-8"))["wrapped_key"] == record["wrapped_key"]
    # The squatter file is untouched.
    assert jsonlib.loads(squatter.read_text(encoding="utf-8"))["wrapped_key"] == other["wrapped_key"]


def test_export_key_file_store_failure_keeps_existing_canonical(tmp_path):
    """When the record store is unwritable the export fails loudly and the
    existing canonical record is NOT overwritten."""
    import json as jsonlib

    from app.backup_crypto import export_public_record
    from app.backup_workflow import KEY_FILE_NAME, export_key_file

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    canonical = backup_root / KEY_FILE_NAME
    old_record, _ = _standalone_install_record("synthetic-passphrase-old")
    canonical.write_text(jsonlib.dumps(old_record), encoding="utf-8")

    engine, factory = _key_export_session(backup_root, "synthetic-passphrase-alice")
    backup_root.chmod(0o555)
    try:
        with factory() as db:
            with pytest.raises(OSError):
                export_key_file(db)
    finally:
        backup_root.chmod(0o755)
        engine.dispose()
    # The only usable record was not silently overwritten.
    assert jsonlib.loads(canonical.read_text(encoding="utf-8"))["wrapped_key"] == old_record["wrapped_key"]


def test_export_key_file_same_record_and_unreadable_existing(tmp_path):
    import json as jsonlib

    from app.backup_crypto import export_public_record
    from app.backup_workflow import KEY_FILE_NAME, PRESERVED_KEY_FILE_PREFIX, export_key_file

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    engine, factory = _key_export_session(backup_root, "synthetic-passphrase-alice")
    with factory() as db:
        record_a = export_public_record(db)
        export_key_file(db)
        export_key_file(db)  # identical record: one content-addressed copy only
    engine.dispose()
    stored = list(backup_root.glob(f"{PRESERVED_KEY_FILE_PREFIX}*.json"))
    assert len(stored) == 1
    assert jsonlib.loads(stored[0].read_text(encoding="utf-8"))["wrapped_key"] == record_a["wrapped_key"]

    # An unreadable existing canonical file is replaced; its content was never
    # a usable recovery record, so nothing is preserved from it.
    canonical = backup_root / KEY_FILE_NAME
    canonical.write_text("not json", encoding="utf-8")
    engine_b, factory_b = _key_export_session(backup_root, "synthetic-passphrase-bob")
    with factory_b() as db:
        record_b = export_public_record(db)
        export_key_file(db)
    engine_b.dispose()
    assert jsonlib.loads(canonical.read_text(encoding="utf-8"))["wrapped_key"] == record_b["wrapped_key"]
    stored_keys = {
        jsonlib.loads(p.read_text(encoding="utf-8"))["wrapped_key"]
        for p in backup_root.glob(f"{PRESERVED_KEY_FILE_PREFIX}*.json")
    }
    assert stored_keys == {record_a["wrapped_key"], record_b["wrapped_key"]}


def test_export_key_file_preservation_is_stable_across_alternating_installs(tmp_path):
    from app.backup_workflow import PRESERVED_KEY_FILE_PREFIX, export_key_file

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    engine_a, factory_a = _key_export_session(backup_root, "synthetic-passphrase-alice")
    engine_b, factory_b = _key_export_session(backup_root, "synthetic-passphrase-bob")
    for factory in (factory_a, factory_b, factory_a, factory_b):
        with factory() as db:
            export_key_file(db)
    engine_a.dispose()
    engine_b.dispose()
    # Exactly one preserved record per install, never duplicated or rewritten.
    assert len(list(backup_root.glob(f"{PRESERVED_KEY_FILE_PREFIX}*.json"))) == 2


def _prepare_encrypted_recovery_target(
    monkeypatch,
    tmp_path,
    passphrase_a: str = "synthetic-passphrase-alice",
    passphrase_b: str = "synthetic-passphrase-bob",
):
    """Fresh-install target (no encryption record) plus a shared backup root."""
    import json as jsonlib

    from sqlalchemy.orm import sessionmaker

    import app.backup_workflow as backup_workflow
    import app.db as db_module
    from app.backup_crypto import encrypt_file
    from app.models import SystemSetting

    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    record_a, key_a = _standalone_install_record(passphrase_a)
    record_b, _ = _standalone_install_record(passphrase_b)

    plain = make_backup(tmp_path / "plain.zip", "manual", datetime(2026, 11, 7, tzinfo=timezone.utc))
    archive = backup_root / "manual" / "archived.zip.enc"
    archive.parent.mkdir(parents=True)
    encrypt_file(key_a, plain, archive)

    # Shared drive: B overwrote the canonical record; A's record was preserved.
    (backup_root / "backup_encryption_key.json").write_text(jsonlib.dumps(record_b), encoding="utf-8")
    digest = hashlib.sha256(record_a["wrapped_key"].encode("utf-8")).hexdigest()[:8]
    (backup_root / f"backup_encryption_key.prev-{digest}.json").write_text(
        jsonlib.dumps(record_a), encoding="utf-8"
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'target.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add(SystemSetting(key="backup_root", value=str(backup_root), updated_by="synthetic-admin"))

    snap = make_backup(
        backup_root / "pre_restore" / "snap.zip",
        "pre_restore",
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

    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(backup_workflow, "create_backup", fake_create_backup)
    monkeypatch.setattr(backup_workflow, "_restore_archive_contents", fake_restore)
    return {
        "engine": engine,
        "archive": archive,
        "key_a": key_a,
        "captured": captured,
    }


def test_restore_selects_preserved_key_record(tmp_path, monkeypatch):
    context = _prepare_encrypted_recovery_target(monkeypatch, tmp_path)
    try:
        restore_backup(
            context["archive"],
            sha256_file(context["archive"]),
            actor="synthetic-admin",
            reason="synthetic preserved-key restore",
            passphrase="synthetic-passphrase-alice",
        )
        # The preserved record opens the archive, not the overwritten canonical one.
        assert context["captured"][0] == (context["archive"].name, context["key_a"])
    finally:
        context["engine"].dispose()


def test_restore_wrong_passphrase_rejected_before_restore(tmp_path, monkeypatch):
    from app.backup_crypto import WrongPassphraseError

    context = _prepare_encrypted_recovery_target(monkeypatch, tmp_path)
    try:
        with pytest.raises(WrongPassphraseError):
            restore_backup(
                context["archive"],
                sha256_file(context["archive"]),
                actor="synthetic-admin",
                reason="synthetic wrong-passphrase restore",
                passphrase="synthetic-passphrase-wrong",
            )
        assert context["captured"] == []  # nothing was restored or snapshotted
    finally:
        context["engine"].dispose()


def test_restore_same_passphrase_selects_record_that_opens_archive(tmp_path, monkeypatch):
    # Two installs sharing one passphrase still have different data keys, so
    # BOTH records unwrap successfully; only the archive probe can pick the
    # record whose key actually opens the archive (not the canonical one).
    context = _prepare_encrypted_recovery_target(
        monkeypatch,
        tmp_path,
        passphrase_a="synthetic-passphrase-shared",
        passphrase_b="synthetic-passphrase-shared",
    )
    try:
        restore_backup(
            context["archive"],
            sha256_file(context["archive"]),
            actor="synthetic-admin",
            reason="synthetic same-passphrase restore",
            passphrase="synthetic-passphrase-shared",
        )
        assert context["captured"][0] == (context["archive"].name, context["key_a"])
    finally:
        context["engine"].dispose()


def test_restore_without_matching_key_record_fails_closed(tmp_path, monkeypatch):
    context = _prepare_encrypted_recovery_target(monkeypatch, tmp_path)
    try:
        # Bob's passphrase unwraps the canonical record, but that record's data
        # key does not open Alice's archive: fail before any restore begins.
        with pytest.raises(ValueError, match="None of the preserved backup key records"):
            restore_backup(
                context["archive"],
                sha256_file(context["archive"]),
                actor="synthetic-admin",
                reason="synthetic no-matching-record restore",
                passphrase="synthetic-passphrase-bob",
            )
        assert context["captured"] == []
    finally:
        context["engine"].dispose()


def test_store_key_record_parallel_writers_forced_prefix_collision(tmp_path, monkeypatch):
    """Parallel writers whose records share a forced 8-char digest prefix must
    ALL land in the store: O_EXCL slot claims + name lengthening, no lost update."""
    import json as jsonlib
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import app.backup_workflow as backup_workflow

    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    real_sha256 = hashlib.sha256

    class FixedPrefixDigest:
        """Same first 8 hex chars for every input; the rest stays real."""

        def __init__(self, *args, **kwargs):
            self._inner = real_sha256(*args, **kwargs)

        def hexdigest(self):
            return "aa" * 4 + self._inner.hexdigest()[8:]

    monkeypatch.setattr(backup_workflow.hashlib, "sha256", FixedPrefixDigest)

    records = [
        {"version": 1, "wrapped_key": f"{i:064x}", "kdf_salt": "00" * 16, "wrap_nonce": "00" * 12}
        for i in range(8)
    ]
    barrier = threading.Barrier(len(records))

    def store(record):
        barrier.wait(timeout=30)
        return backup_workflow._store_key_record(backup_root, record)

    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        list(pool.map(store, records))

    stored = {}
    for path in backup_root.glob("backup_encryption_key.prev-*.json"):
        payload = jsonlib.loads(path.read_text(encoding="utf-8"))
        stored[payload["wrapped_key"]] = path.name
    assert set(stored) == {record["wrapped_key"] for record in records}


def test_store_key_record_parallel_same_record_single_file(tmp_path):
    """Concurrent exports of the SAME record stay idempotent: one slot file."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import app.backup_workflow as backup_workflow

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    record = {"version": 1, "wrapped_key": "ab" * 48, "kdf_salt": "00" * 16, "wrap_nonce": "00" * 12}
    barrier = threading.Barrier(8)

    def store(_):
        barrier.wait(timeout=30)
        return backup_workflow._store_key_record(backup_root, record)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store, range(8)))

    assert len(list(backup_root.glob("backup_encryption_key.prev-*.json"))) == 1


def test_store_key_record_torn_slot_is_repaired_under_longer_name(tmp_path):
    """A torn slot file (crashed writer / external corruption) must not block
    repair: the record is re-stored under a longer name, torn file untouched."""
    import json as jsonlib

    import app.backup_workflow as backup_workflow

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    record = {"version": 1, "wrapped_key": "cd" * 48, "kdf_salt": "00" * 16, "wrap_nonce": "00" * 12}
    digest = hashlib.sha256(record["wrapped_key"].encode("utf-8")).hexdigest()
    torn = backup_root / f"backup_encryption_key.prev-{digest[:8]}.json"
    torn.write_text('{"version": 1, "wrap', encoding="utf-8")  # torn mid-write

    stored_path = backup_workflow._store_key_record(backup_root, record)

    assert stored_path.name == f"backup_encryption_key.prev-{digest[:16]}.json"
    assert jsonlib.loads(stored_path.read_text(encoding="utf-8"))["wrapped_key"] == record["wrapped_key"]
    assert torn.read_text(encoding="utf-8") == '{"version": 1, "wrap'  # never modified
    records, saw_file, saw_unreadable = backup_workflow._recovery_key_records(backup_root)
    assert [r["wrapped_key"] for r in records] == [record["wrapped_key"]]
    assert saw_file and saw_unreadable  # torn file skipped but reported


def _no_hardlinks(*args, **kwargs):
    raise OSError("hard links unsupported (simulated exFAT)")


def test_store_key_record_without_hardlinks_parallel_same_record(tmp_path, monkeypatch):
    """exFAT-style fallback: exclusive-create claims + reader retry keep
    concurrent same-record exports idempotent without hard links."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import app.backup_workflow as backup_workflow

    monkeypatch.setattr(backup_workflow.os, "link", _no_hardlinks)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    record = {"version": 1, "wrapped_key": "ab" * 48, "kdf_salt": "00" * 16, "wrap_nonce": "00" * 12}
    barrier = threading.Barrier(4)

    def store(_):
        barrier.wait(timeout=30)
        return backup_workflow._store_key_record(backup_root, record)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(store, range(4)))

    files = list(backup_root.glob("backup_encryption_key.prev-*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["wrapped_key"] == record["wrapped_key"]


def test_store_key_record_without_hardlinks_forced_collision(tmp_path, monkeypatch):
    """exFAT-style fallback under a forced shared 8-hex prefix: every distinct
    record still lands, no clobber."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import app.backup_workflow as backup_workflow

    monkeypatch.setattr(backup_workflow.os, "link", _no_hardlinks)
    real_sha256 = hashlib.sha256

    class FixedPrefixDigest:
        def __init__(self, *args, **kwargs):
            self._inner = real_sha256(*args, **kwargs)

        def hexdigest(self):
            return "bb" * 4 + self._inner.hexdigest()[8:]

    monkeypatch.setattr(backup_workflow.hashlib, "sha256", FixedPrefixDigest)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    records = [
        {"version": 1, "wrapped_key": f"{i:064x}", "kdf_salt": "00" * 16, "wrap_nonce": "00" * 12}
        for i in range(6)
    ]
    barrier = threading.Barrier(len(records))

    def store(record):
        barrier.wait(timeout=30)
        return backup_workflow._store_key_record(backup_root, record)

    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        list(pool.map(store, records))

    stored = set()
    for path in backup_root.glob("backup_encryption_key.prev-*.json"):
        stored.add(json.loads(path.read_text(encoding="utf-8"))["wrapped_key"])
    assert stored == {record["wrapped_key"] for record in records}


def test_read_record_file_tolerates_briefly_partial_slot(tmp_path):
    """A reader overlapping a direct-write (fallback) publication retries
    briefly instead of mistaking the partial file for a different record."""
    import threading
    import time as time_mod

    import app.backup_workflow as backup_workflow

    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    slot = backup_root / "backup_encryption_key.prev-12345678.json"
    full = json.dumps({"version": 1, "wrapped_key": "ef" * 48}, ensure_ascii=False, indent=2)
    slot.write_text(full[:10], encoding="utf-8")  # partial publication

    def finish_write():
        time_mod.sleep(0.03)
        slot.write_text(full, encoding="utf-8")

    writer = threading.Thread(target=finish_write)
    writer.start()
    record = backup_workflow._read_record_file(slot)
    writer.join()
    assert record is not None and record["wrapped_key"] == "ef" * 48
