"""9ت: interruption and power-loss behaviour of create_backup (no real pg_dump, no PostgreSQL).

pg_dump is replaced by a fake that writes fixed synthetic bytes. Failures are injected by
monkeypatching, so these tests show failure bookkeeping and write ordering only. They do not
prove behaviour of a real power cut, a real Windows volume or a real restore.
"""
import os
import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import backup_workflow as bw
from app.db import Base
from app.models import BackupRun, SystemSetting

KEY = bytes(range(32))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    root = tmp_path / "backups"
    root.mkdir()
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "e.jpg").write_bytes(b"synthetic photo")

    def fake_run(command, env_vars, *, cwd=None):
        if "--file" in command:
            Path(command[command.index("--file") + 1]).write_bytes(b"synthetic dump bytes" * 50)

    monkeypatch.setattr(bw, "PHOTO_DIRECTORY", photos)
    monkeypatch.setattr(bw, "has_passphrase", lambda db: True)
    monkeypatch.setattr(bw, "load_data_key", lambda db: KEY)
    monkeypatch.setattr(bw, "export_key_file", lambda db: None)
    monkeypatch.setattr(bw, "validate_backup_root", lambda value, require_external=True: Path(value))
    monkeypatch.setattr(bw, "_find_pg_tool", lambda name: name)
    monkeypatch.setattr(bw, "_database_args", lambda: ([], {}))
    monkeypatch.setattr(bw, "_run", fake_run)
    monkeypatch.setattr(bw, "_schema_revision", lambda db: "synthetic")
    with Session(engine) as db:
        db.add(SystemSetting(key=bw.BACKUP_SETTING_KEY, value=str(root), updated_by="synthetic-admin"))
        db.flush()
        yield db, root
    Base.metadata.drop_all(engine)
    engine.dispose()


def _files(root):
    return [p for p in root.rglob("*") if p.is_file()]


def _temp_dirs(root):
    return [p for p in root.iterdir() if p.name.startswith("myhotel_backup_")]


def test_successful_run_is_recorded_and_archive_verifies(env):
    db, root = env
    run = bw.create_backup(db, "manual", "synthetic-admin")
    assert run.status == "success" and run.sha256 and run.size_bytes > 0
    assert Path(run.archive_path).is_file() and not _temp_dirs(root)
    assert bw.verify_backup_archive(run.archive_path, run.sha256, KEY)["format_version"] == 1


@pytest.mark.parametrize("stage", ["encrypt", "replace"])
def test_interruption_before_the_archive_is_published_leaves_no_file_and_a_failed_record(env, monkeypatch, stage):
    db, root = env
    if stage == "encrypt":
        def boom(*args, **kwargs):
            raise OSError("synthetic interruption during encryption")
        monkeypatch.setattr(bw, "encrypt_file", boom)
    else:
        def boom(src, dst):
            raise OSError("synthetic interruption before publish")
        monkeypatch.setattr(bw.os, "replace", boom)
    run = bw.create_backup(db, "manual", "synthetic-admin")
    assert run.status == "failed" and "synthetic interruption" in run.error_message
    assert run.archive_path is None and run.sha256 is None and run.size_bytes == 0
    assert _files(root) == [] and _temp_dirs(root) == []
    assert db.scalar(select(BackupRun).where(BackupRun.status == "success")) is None


def test_failed_integrity_check_after_publish_is_never_recorded_as_success(env, monkeypatch):
    db, root = env

    def refuse(*args, **kwargs):
        raise ValueError("synthetic verification refusal")
    monkeypatch.setattr(bw, "verify_backup_archive", refuse)
    run = bw.create_backup(db, "manual", "synthetic-admin")
    assert run.status == "failed" and run.archive_path is None and run.sha256 is None
    assert _temp_dirs(root) == []
    assert db.scalar(select(BackupRun).where(BackupRun.status == "success")) is None


def _trace(monkeypatch):
    """Record, in order, which fsync'd descriptors were files vs directories, and the replace call."""
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        info = os.fstat(fd)
        events.append(("fsync_dir" if stat.S_ISDIR(info.st_mode) else "fsync_file", info.st_ino, info.st_size))
        return real_fsync(fd)

    def replace(src, dst):
        info = os.stat(src)
        events.append(("replace", info.st_ino, info.st_size))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    return events


def test_archive_bytes_are_fsynced_before_publish_and_directory_after(env, monkeypatch):
    db, root = env
    events = _trace(monkeypatch)
    run = bw.create_backup(db, "manual", "synthetic-admin")
    assert run.status == "success"
    kinds = [e[0] for e in events]
    index = kinds.index("replace")
    _, inode, size = events[index]
    assert size == run.size_bytes
    # the exact file that is published was fsynced in full before the rename
    assert any(k == "fsync_file" and i == inode and s == size for k, i, s in events[:index]), events
    if os.name != "nt":
        assert "fsync_dir" in kinds[index + 1:], events


def test_fsync_failure_is_a_failed_run_not_a_success(env, monkeypatch):
    db, root = env
    real_fsync = os.fsync

    def failing(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic fsync failure")
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", failing)
    run = bw.create_backup(db, "manual", "synthetic-admin")
    assert run.status == "failed" and "synthetic fsync failure" in run.error_message
    assert run.archive_path is None and _files(root) == [] and _temp_dirs(root) == []
