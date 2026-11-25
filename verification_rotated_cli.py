"""Synthetic supported fresh-target CLI restore, no HTTP/admission work."""
import sys,builtins
from pathlib import Path
from datetime import date
from decimal import Decimal
from sqlalchemy import select,text
from app.db import SessionLocal
from app.backup_crypto import set_passphrase,verify_passphrase
from app.models import SystemSetting,Employee,AdminAccount,AdminSession
from app import backup_workflow as b,restore_cli
archive,root=map(Path,sys.argv[1:])
original=b.validate_backup_root
b.validate_backup_root=lambda value,require_external=True:original(value,require_external=False)
b.PHOTO_DIRECTORY=root/'target-photos'
with SessionLocal.begin() as db:
    assert db.scalar(select(Employee.id)) is None
    set_passphrase(db,'synthetic-fresh-target-only','synthetic')
    db.add(SystemSetting(key='backup_root',value=str(root),updated_by='synthetic'))
    db.add(SystemSetting(key='synthetic-target-marker',value='TARGET',updated_by='synthetic'))
    db.add(SystemSetting(key='post_restore_login_proof',value='synthetic-prior-proof',updated_by='synthetic'))
# Actual distinct-target-key post-swap failure must restore target crypto,
# marker and prior proof, not source crypto or half-installed new proof.
from app.backup_crypto import load_data_key,encrypted_archive_accepts_key
with SessionLocal() as db:
    target_key=load_data_key(db)
original_finalize=b._finalize_restored_security

def fail_after_finalize(**kwargs):
    original_finalize(**kwargs)
    raise RuntimeError('Synthetic after proof installation')
b._finalize_restored_security=fail_after_finalize
try:
    try:b.restore_backup(archive,b.sha256_file(archive),actor='synthetic',reason='Synthetic distinct-key proof rollback',passphrase='synthetic-rotated-archive-passphrase')
    except RuntimeError as exc:assert 'previous database was restored' in str(exc)
    else:raise AssertionError('Expected forced rollback')
finally:b._finalize_restored_security=original_finalize
with SessionLocal() as db:
    assert db.get(SystemSetting,'synthetic-target-marker').value=='TARGET'
    assert db.get(SystemSetting,'post_restore_login_proof').value=='synthetic-prior-proof'
    assert verify_passphrase(db,'synthetic-fresh-target-only')
    assert db.scalar(select(AdminAccount.id)) is None
pre=list((root/'pre_restore').glob('*.enc'))
assert pre and all(encrypted_archive_accepts_key(target_key,p) for p in pre)
restore_cli.getpass.getpass=lambda prompt:'synthetic-rotated-archive-passphrase'
builtins.input=lambda prompt:'RESTORE'
assert restore_cli.main(['--archive',str(archive),'--backup-root',str(root),'--lang','en','--actor','synthetic-fresh-cli','--reason','Synthetic separate initialized target rotated archive'])==0
with SessionLocal() as db:
    assert db.get(SystemSetting,'synthetic-business-marker').value=='ARCHIVE'
    assert db.scalar(select(Employee.id).where(Employee.code=='SYN-ARCHIVE')) is not None
    assert db.scalar(text('SELECT count(*) FROM overtime_day_awards'))==2
    assert db.scalar(select(AdminSession.id)) is None
    accounts=db.scalars(select(AdminAccount)).all()
    assert accounts and all(a.must_change_password for a in accounts)
    assert db.get(SystemSetting,'post_restore_passphrase_required') is not None
    assert db.get(SystemSetting,'post_restore_regen_required') is not None
    from app.recovery_login_proof import verify_login_phrase,clear_proof,SETTING_KEY
    from app.backup_crypto import WrongPassphraseError
    assert verify_passphrase(db,'synthetic-only-archive-acceptance')
    assert not verify_passphrase(db,'synthetic-rotated-archive-passphrase')
    verify_login_phrase(db,'synthetic-rotated-archive-passphrase')
    try:verify_login_phrase(db,'synthetic-only-archive-acceptance')
    except WrongPassphraseError:pass
    else:raise AssertionError('Archive-era phrase must not satisfy selected newer proof')
    proof=db.get(SystemSetting,SETTING_KEY).value
    assert 'data_key"' not in proof and 'synthetic-rotated-archive-passphrase' not in proof
    clear_proof(db,actor='synthetic')
    db.flush()
    assert db.get(SystemSetting,SETTING_KEY).value==''
    db.rollback()
    assert db.get(SystemSetting,SETTING_KEY).value==proof
print('PASS separate initialized target actual CLI recovery from old archive using rotated source recovery record; no-account disaster passphrase/password/code-regeneration gates set, current target encryption retained')
