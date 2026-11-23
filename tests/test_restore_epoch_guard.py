"""Restore-epoch guard (bc74a88 follow-up, peer-confirmed id collision).

A request authenticated against the pre-restore database carries the epoch
observed at authentication time. After a restore bumps the epoch, the
credential write lock refuses that request with 409 instead of mutating an
account whose numeric id may now belong to a different user.
"""
import pytest
from fastapi import HTTPException

from app.db import SessionLocal, engine
from app.models import SystemSetting
from app.restore_lock import (
    RESTORE_EPOCH_KEY,
    acquire_credential_write_lock,
    read_restore_epoch,
)

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="restore advisory locks are PostgreSQL-only"
)


def test_epoch_guard_refuses_stale_epoch_and_allows_current():
    with SessionLocal() as db:
        original = read_restore_epoch(db)
    try:
        with SessionLocal.begin() as db:
            db.merge(SystemSetting(key=RESTORE_EPOCH_KEY, value="epoch-A", updated_by="test"))
        with SessionLocal.begin() as db:
            assert read_restore_epoch(db) == "epoch-A"
            # Matching epoch and pre-auth sites (argument omitted) proceed.
            acquire_credential_write_lock(db, expected_epoch="epoch-A")
            acquire_credential_write_lock(db)
        with SessionLocal.begin() as db:
            with pytest.raises(HTTPException) as excinfo:
                acquire_credential_write_lock(db, expected_epoch="pre-restore-epoch")
            assert excinfo.value.status_code == 409
            # None is a real epoch ("never restored"): once a restore bumped
            # the epoch, a pre-restore-authenticated request carrying None is
            # refused too.
            with pytest.raises(HTTPException) as excinfo:
                acquire_credential_write_lock(db, expected_epoch=None)
            assert excinfo.value.status_code == 409
    finally:
        with SessionLocal.begin() as db:
            row = db.get(SystemSetting, RESTORE_EPOCH_KEY)
            if original is None:
                if row is not None:
                    db.delete(row)
            else:
                db.merge(SystemSetting(key=RESTORE_EPOCH_KEY, value=original, updated_by="test"))
