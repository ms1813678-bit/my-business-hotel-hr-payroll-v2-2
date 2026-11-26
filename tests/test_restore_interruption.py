"""9ت: restore interruption bookkeeping. Reuses the existing synthetic restore harness.

The archive swap itself is replaced by a stub, as in test_backup_workflow.py. These tests cover
failure handling and records only. They do not prove a real pg_restore, a killed process, a
power cut or Windows behaviour.
"""
import pytest
from sqlalchemy import select

import app.backup_workflow as bw
from app.models import BackupRun
from tests.test_backup_workflow import _prepare_restore_test_environment
from app.backup_workflow import restore_backup, sha256_file


def _call(context, reason):
    return restore_backup(context["selected_archive"], context["selected_hash"], actor="synthetic-admin", reason=reason)


def test_failure_after_the_swap_during_security_finalization_rolls_back_and_records_no_history(monkeypatch, tmp_path):
    context = _prepare_restore_test_environment(monkeypatch, tmp_path)
    try:
        def interrupted(**kwargs):
            raise RuntimeError("synthetic interruption in finalization")
        monkeypatch.setattr(bw, "_finalize_restored_security", interrupted)
        with pytest.raises(RuntimeError, match="previous database was restored"):
            _call(context, "Synthetic finalization interruption")
        assert context["restore_calls"] == [context["selected_archive"], context["pre_restore_archive"]]
        assert not (context["backup_root"] / "restore_history.jsonl").exists()
        with context["factory"]() as db:
            actions = db.scalars(select(context["AuditLog"].action)).all()
        assert actions == ["backup_restore_rolled_back"]
    finally:
        context["engine"].dispose()


def test_rollback_that_also_fails_preserves_the_pre_restore_archive_and_says_so(monkeypatch, tmp_path):
    context = _prepare_restore_test_environment(monkeypatch, tmp_path)
    try:
        before = sha256_file(context["pre_restore_archive"])
        calls = []

        def always_fails(path, root, expected_sha256=None, data_key=None, **kwargs):
            calls.append(path)
            raise RuntimeError("synthetic failure for " + path.name)
        monkeypatch.setattr(bw, "_restore_archive_contents", always_fails)
        with pytest.raises(RuntimeError) as caught:
            _call(context, "Synthetic double failure")
        message = str(caught.value)
        assert "Automatic rollback also failed" in message
        assert str(context["pre_restore_archive"]) in message and "Preserve pre-restore archive" in message
        assert calls == [context["selected_archive"], context["pre_restore_archive"]]
        # the only copy of the previous database is intact and the selected archive is unchanged
        assert context["pre_restore_archive"].is_file() and sha256_file(context["pre_restore_archive"]) == before
        assert sha256_file(context["selected_archive"]) == context["selected_hash"]
        assert not (context["backup_root"] / "restore_history.jsonl").exists()
        with context["factory"]() as db:
            actions = db.scalars(select(context["AuditLog"].action)).all()
            runs = db.scalars(select(BackupRun).where(BackupRun.backup_type == "pre_restore")).all()
        # no success or rolled-back audit row is claimed when nothing was restored
        assert "backup_restored" not in actions and "backup_restore_rolled_back" not in actions
        assert len(runs) == 1 and runs[0].archive_path == str(context["pre_restore_archive"])
    finally:
        context["engine"].dispose()


def test_pre_restore_backup_failure_cancels_before_any_swap(monkeypatch, tmp_path):
    context = _prepare_restore_test_environment(monkeypatch, tmp_path)
    try:
        def failed(db, backup_type, actor):
            run = BackupRun(backup_type="pre_restore", status="failed", size_bytes=0, initiated_by=actor, error_message="synthetic")
            db.add(run)
            db.flush()
            return run
        monkeypatch.setattr(bw, "create_backup", failed)
        with pytest.raises(RuntimeError, match="restore was cancelled"):
            _call(context, "Synthetic cancelled restore")
        assert context["restore_calls"] == []
        assert not (context["backup_root"] / "restore_history.jsonl").exists()
    finally:
        context["engine"].dispose()
