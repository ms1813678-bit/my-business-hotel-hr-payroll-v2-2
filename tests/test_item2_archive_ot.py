"""Standalone actual encrypted backup/restore on dedicated synthetic DB only."""
import os
from pathlib import Path
from datetime import datetime,UTC,date
from decimal import Decimal
import json

import pytest
from sqlalchemy import select,text

pytestmark=pytest.mark.skipif(os.environ.get('MANUAL_ARCHIVE_ROUNDTRIP')!='1',reason='Dedicated disposable DB and compatible pg clients required')


def test_actual_encrypted_archive_preserves_ot_chain_monthly_and_rollback(tmp_path,monkeypatch):
    from app.config import settings
    from app.db import SessionLocal,engine
    from app.models import SystemSetting,AdminAccount,AdminSession,AuditLog,LeaveSyntheticTransition,LeaveSyntheticTransitionEntry,LeaveBalanceTransaction,Department,Employee,LeaveType
    from app import backup_workflow as b
    from app.backup_crypto import set_passphrase,load_data_key
    from app.restore_lock import read_restore_epoch
    assert engine.url.database.startswith('synthetic_archive_')
    backup_root=tmp_path/'backups';backup_root.mkdir()
    photos=tmp_path/'photos';photos.mkdir()
    monkeypatch.setattr(b,'PHOTO_DIRECTORY',photos)
    # Test disk not external. All archive crypto/dump/restore/security code real.
    original_validate=b.validate_backup_root
    monkeypatch.setattr(b,'validate_backup_root',lambda value,require_external=True:original_validate(value,require_external=False))
    # Synthetic test material only, never a user account secret.
    phrase='synthetic-only-archive-acceptance'
    with SessionLocal.begin() as db:
        db.add(SystemSetting(key='backup_root',value=str(backup_root),updated_by='synthetic'))
        set_passphrase(db,phrase,'synthetic')
        common=AdminAccount(username='synthetic-common',password_hash='synthetic-archive-hash',role='system_admin',active=True)
        archive_only=AdminAccount(username='synthetic-archive-only',password_hash='synthetic-obsolete-hash',role='assistant_manager',active=True)
        db.add_all((common,archive_only));db.flush()
        db.add(AdminSession(id='synthetic-old-session',admin_account_id=common.id))
        db.add(SystemSetting(key='synthetic-business-marker',value='ARCHIVE',updated_by='synthetic'))
        department=Department(name='Synthetic archive department',active=True);db.add(department);db.flush()
        employee=Employee(code='SYN-ARCHIVE',full_name='Synthetic archive only',department_id=department.id,position='Synthetic',hire_date=date(2026,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8));db.add(employee);db.flush()
        kind=db.scalar(select(LeaveType).where(LeaveType.code=='annual'))
        ledger=LeaveBalanceTransaction(employee_id=employee.id,leave_type_id=kind.id,transaction_type='adjustment',days_delta=Decimal('-0.123456789012345678901234567890123456789'),reason='Synthetic exact archive fixture',created_by='synthetic');db.add(ledger);db.flush()
        db.add(LeaveSyntheticTransition(id='a'*64,scope_key='b'*64,actor_id=common.id,session_id='synthetic-old-session',session_version=common.session_version,restore_epoch=None,request_digest='c'*64,result_json='[]',post_reconciliation_digest='d'*64,created_at=datetime.now(UTC)));db.flush()
        db.add(LeaveSyntheticTransitionEntry(operation_id='a'*64,employee_id=employee.id,code='A',ledger_id=ledger.id,ledger_digest='e'*64))
        employee_id=employee.id; ledger_id=ledger.id
    from app.models import PayrollRun,PayrollItem,AttendanceDailyRecord
    from app.overtime_day_award import prepare_day_award,confirm_day_award,stage_monthly_totals
    with SessionLocal.begin() as db:
        run=PayrollRun(period_start=date(2026,2,1),period_end=date(2026,2,28),pay_date=date(2026,3,5),status='draft',created_by='synthetic');db.add(run);db.flush()
        item=PayrollItem(payroll_run_id=run.id,employee_id=employee_id,employee_code_snapshot='SYN-ARCHIVE',employee_name_snapshot='Synthetic',department_name_snapshot='Synthetic',position_snapshot='Synthetic',salary_basis=6000,base_salary=6000,daily_hours=8,performance_bonus=1000);db.add(item);db.flush();item_id=item.id
        db.add(AttendanceDailyRecord(employee_id=employee_id,work_date=date(2026,2,2),status='approved',scheduled_start_at=datetime(2026,2,2,9),scheduled_end_at=datetime(2026,2,2,17),approved_clock_in=datetime(2026,2,2,9),approved_clock_out=datetime(2026,2,2,19),worked_minutes=600))
    with SessionLocal() as db:
        common=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-common'))
        auth=dict(actor_id=common.id,session_id='synthetic-old-session',session_version=common.session_version,expected_epoch=None)
    for key,hours,predecessor in [('1',Decimal(3),None),('2',Decimal(2),'1'*64)]:
        with SessionLocal.begin() as db:
            row=prepare_day_award(db,operation_id=key*64,item_id=item_id,work_date=date(2026,2,2),manual_hours=hours,rate_divisor=9,reason='Synthetic archive correction chain',predecessor_id=predecessor,**auth)
        with SessionLocal.begin() as db:confirm_day_award(db,receipt_id=key*64,human_confirmed=True,**auth)
    with SessionLocal.begin() as db:stage_monthly_totals(db,item_id,rounding_contract='existing-monthly-half-up')
    def ot_snapshot():
        with SessionLocal() as db:
            return {name:[tuple(str(v) for v in row) for row in db.execute(text('SELECT * FROM '+name+' ORDER BY 1')).all()] for name in ('overtime_day_review_receipts','overtime_day_awards','payroll_items')}
    ot_before=ot_snapshot()
    (photos/'synthetic.jpg').write_bytes(b'synthetic archive photo')
    with SessionLocal.begin() as db:
        saved=b.create_backup(db,'manual','synthetic')
        assert saved.status=='success',saved.error_message
        archive=Path(saved.archive_path);archive_sha=saved.sha256
        manifest=b.verify_backup_archive(archive,archive_sha,load_data_key(db))
        assert manifest['schema_revision']=='0065_overtime_day_review'
        assert archive.suffix=='.enc' and manifest['employee_photo_count']==1
    with SessionLocal.begin() as db:
        db.get(SystemSetting,'synthetic-business-marker').value='CURRENT'
        common=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-common'))
        common.password_hash='synthetic-current-hash';common.session_version+=3
        version=common.session_version
        # Simulate a distinct current identity set without bypassing the host's
        # deliberately immutable account-deletion guards.
        other=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-archive-only'))
        other.username='synthetic-current-only'
        other.password_hash='synthetic-current-only-hash'

    (photos/'synthetic.jpg').write_bytes(b'synthetic current photo')
    result=b.restore_backup(archive,archive_sha,actor='synthetic-common',reason='Synthetic actual roundtrip',passphrase=phrase)
    assert Path(result['pre_restore_archive']).is_file()
    with SessionLocal() as db:
        assert db.get(SystemSetting,'synthetic-business-marker').value=='ARCHIVE'
        common=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-common'))
        assert common.password_hash=='synthetic-current-hash' and common.session_version>=version
        archived=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-archive-only'))
        assert not archived.active and archived.must_change_password and archived.pending_credential_reset
        assert db.scalar(select(AdminSession.id)) is None
        assert read_restore_epoch(db) is not None
        assert db.get(LeaveSyntheticTransition,'a'*64) is not None
        assert db.get(LeaveSyntheticTransitionEntry,('a'*64,employee_id,'A')).ledger_id==ledger_id
        assert db.get(LeaveBalanceTransaction,ledger_id).days_delta==Decimal('-0.123456789012345678901234567890123456789')
    assert ot_snapshot()==ot_before
    assert (photos/'synthetic.jpg').read_bytes()==b'synthetic archive photo'
    # Inject AFTER real schema/photo replacement, to test real rollback archive.
    with SessionLocal.begin() as db:db.get(SystemSetting,'synthetic-business-marker').value='PRE-FAILURE'
    (photos/'synthetic.jpg').write_bytes(b'synthetic pre-failure photo')
    def fail(**kw):raise RuntimeError('Synthetic post-swap finalization failure')
    monkeypatch.setattr(b,'_finalize_restored_security',fail)
    with pytest.raises(RuntimeError,match='previous database was restored'):
        b.restore_backup(archive,archive_sha,actor='synthetic-common',reason='Synthetic post-swap rollback',passphrase=phrase)
    with SessionLocal() as db:
        assert db.get(SystemSetting,'synthetic-business-marker').value=='PRE-FAILURE'
        common=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-common'))
        assert common.password_hash=='synthetic-current-hash'
        assert db.scalar(select(AuditLog.id).where(AuditLog.action=='backup_restore_rolled_back')) is not None
    assert ot_snapshot()==ot_before
    assert (photos/'synthetic.jpg').read_bytes()==b'synthetic pre-failure photo'
    assert archive.is_file()
