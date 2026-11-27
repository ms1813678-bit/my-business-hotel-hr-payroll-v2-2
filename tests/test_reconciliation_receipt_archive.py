"""Manual nonempty0064 archive acceptance on prior synthetic HTTP fixture DB."""
import os
from pathlib import Path
import pytest
from sqlalchemy import select
from app.db import SessionLocal,engine
from app.models import LeaveReconciliationReviewReceipt,LeaveReconciliationApproval,SystemSetting,AdminAccount,AdminSession
from app.leave_reconciliation_receipt import receipt_hash

def _alembic_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root=Path(__file__).resolve().parents[1]
    config=Config(str(root/'alembic.ini'));config.set_main_option('script_location',str(root/'alembic'))
    return ScriptDirectory.from_config(config).get_current_head()


pytestmark=pytest.mark.skipif(os.getenv('MANUAL_RECEIPT_ARCHIVE')!='1',reason='Dedicated completed synthetic HTTP fixture DB only')


def test_nonempty_receipt_actual_archive_restore_rollback(tmp_path,monkeypatch):
    assert engine.url.database.startswith('synthetic_cycle_')
    from app import backup_workflow as b
    from app.backup_crypto import set_passphrase,load_data_key
    from app.restore_lock import read_restore_epoch
    root=tmp_path/'backups';root.mkdir();photos=tmp_path/'photos';photos.mkdir()
    monkeypatch.setattr(b,'PHOTO_DIRECTORY',photos)
    validate=b.validate_backup_root
    monkeypatch.setattr(b,'validate_backup_root',lambda p,require_external=True:validate(p,require_external=False))
    with SessionLocal.begin() as db:
        rows=db.scalars(select(LeaveReconciliationReviewReceipt)).all()
        assert len(rows)>=1
        receipt_id=rows[0].id;integrity=rows[0].integrity_digest;assert receipt_hash(rows[0])==integrity
        from app.leave_reconciliation_approval import approval_hash
        approval=db.scalar(select(LeaveReconciliationApproval))
        assert approval and approval_hash(approval)==approval.integrity_digest
        approval_id=approval.id;approval_integrity=approval.integrity_digest
        db.add(SystemSetting(key='backup_root',value=str(root),updated_by='synthetic'))
        db.add(SystemSetting(key='synthetic-receipt-archive-marker',value='ARCHIVE',updated_by='synthetic'))
        set_passphrase(db,'synthetic-only-receipt-archive','synthetic')
    with SessionLocal.begin() as db:
        backup=b.create_backup(db,'manual','synthetic-workbook-store')
        assert backup.status=='success',backup.error_message
        archive=backup.archive_path;digest=backup.sha256
        assert b.verify_backup_archive(Path(archive),digest,load_data_key(db))['schema_revision']==_alembic_head()
    with SessionLocal.begin() as db:
        db.get(SystemSetting,'synthetic-receipt-archive-marker').value='CURRENT'
        actor=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-workbook-store'))
        actor.password_hash='synthetic-current-credential';actor.session_version+=2
    b.restore_backup(archive,digest,actor='synthetic-workbook-store',reason='Synthetic nonempty0064 acceptance',passphrase='synthetic-only-receipt-archive')
    with SessionLocal() as db:
        approval=db.get(LeaveReconciliationApproval,approval_id)
        assert approval.integrity_digest==approval_integrity and approval_hash(approval)==approval_integrity
        row=db.get(LeaveReconciliationReviewReceipt,receipt_id)
        assert row.integrity_digest==integrity and receipt_hash(row)==integrity
        assert row.restore_epoch!=read_restore_epoch(db)  # historical evidence, not current authority
        assert db.get(SystemSetting,'synthetic-receipt-archive-marker').value=='ARCHIVE'
        assert db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-workbook-store')).password_hash=='synthetic-current-credential'
        assert db.scalar(select(AdminSession.id)) is None
    with SessionLocal.begin() as db:db.get(SystemSetting,'synthetic-receipt-archive-marker').value='PREFAILURE'
    def fail(**kw):raise RuntimeError('Synthetic0064 post-swap finalization failure')
    monkeypatch.setattr(b,'_finalize_restored_security',fail)
    with pytest.raises(RuntimeError,match='previous database was restored'):
        b.restore_backup(archive,digest,actor='synthetic-workbook-store',reason='Synthetic0064 rollback',passphrase='synthetic-only-receipt-archive')
    with SessionLocal() as db:
        approval=db.get(LeaveReconciliationApproval,approval_id)
        assert approval.integrity_digest==approval_integrity and approval_hash(approval)==approval_integrity
        row=db.get(LeaveReconciliationReviewReceipt,receipt_id)
        assert row.integrity_digest==integrity and receipt_hash(row)==integrity
        assert db.get(SystemSetting,'synthetic-receipt-archive-marker').value=='PREFAILURE'
        assert db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-workbook-store')).password_hash=='synthetic-current-credential'
