"""Actual rotated-source archive to separate initialized install, CLI path."""
import json,os,subprocess,sys,hashlib
from pathlib import Path
import pytest
from tests.test_rotation_source_cycle import test_actual_encrypted_archive_preserves_ot_chain_monthly_and_rollback as source_cycle

@pytest.mark.skipif(os.environ.get('MANUAL_ARCHIVE_ROUNDTRIP')!='1',reason='Two dedicated disposable DBs required')
def test_rotated_archive_cli_separate_fresh_install(tmp_path,monkeypatch):
    source_cycle(tmp_path,monkeypatch)
    root=tmp_path/'backups'
    archives=list((root/'manual').glob('*.enc'))
    assert len(archives)==1
    # Canonical file was re-exported by latest real restore. The source cycle's
    # successful rotated-phrase restore preserved current wrapping record.
    from app.backup_crypto import unwrap_data_key_from_record,encrypted_archive_accepts_key
    candidates=[]
    for p in root.glob('*.json'):
        try:
            record=json.loads(p.read_text());key=unwrap_data_key_from_record(record,'synthetic-rotated-archive-passphrase')
            if encrypted_archive_accepts_key(key,archives[0]):candidates.append(record)
        except Exception:pass
    assert candidates
    recovery=tmp_path/'fresh-install-recovery';recovery.mkdir()
    import shutil
    archive=recovery/archives[0].name;shutil.copy2(archives[0],archive)
    (recovery/'backup_encryption_key.json').write_text(json.dumps(candidates[0]))
    script=Path(__file__).resolve().parents[1]/'verification_rotated_cli.py'
    env=dict(os.environ,DATABASE_URL=os.environ['ROTATION_TARGET_DATABASE_URL'])
    result=subprocess.run([sys.executable,str(script),str(archive),str(recovery)],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    print(result.stdout)
