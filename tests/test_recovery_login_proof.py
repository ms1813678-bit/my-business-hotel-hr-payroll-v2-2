"""One-use proof tests, existing route fixture, no admission experiment."""
import json,re
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import SystemSetting,AdminSession
from app.backup_crypto import set_passphrase,export_public_record,load_data_key
from app.recovery_login_proof import install_proof,verify_login_phrase,SETTING_KEY
from tests.test_restore_reconciliation import factory,ADMIN_PW
from sqlalchemy import select,create_engine
from sqlalchemy.orm import Session
from app.db import Base


@pytest.fixture(autouse=True)
def sqlite_epoch_reader(monkeypatch):
    monkeypatch.setattr('app.recovery_login_proof.read_restore_epoch',lambda db: db.get(SystemSetting,'restore_epoch').value)

def prepare(factory):
    with factory.begin() as db:
        set_passphrase(db,'synthetic-archive-phrase-A','synthetic')
    other=create_engine('sqlite://')
    Base.metadata.create_all(other)
    with Session(other) as source:
        set_passphrase(source,'synthetic-recovery-phrase-B','synthetic')
        record=export_public_record(source);key=load_data_key(source)
    other.dispose()
    with factory.begin() as db:
        db.merge(SystemSetting(key='restore_epoch',value='epoch-new',updated_by='synthetic'))
        db.merge(SystemSetting(key='post_restore_passphrase_required',value='armed',updated_by='synthetic'))
        db.merge(SystemSetting(key='post_restore_regen_required',value='armed',updated_by='synthetic'))
        from app.models import AdminAccount
        db.get(AdminAccount,1).must_change_password=True
        install_proof(db,record=record,data_key=key,archive_sha256='a'*64,restore_epoch='epoch-new',actor='synthetic')
    return record,key


def csrf(response):
    return re.search(r'name="csrf_token" value="([^"]+)"',response.text).group(1)


def test_login_requires_password_and_selected_phrase_then_consumes_atomic(factory):
    prepare(factory)
    with TestClient(app) as client:
        page=client.get('/login')
        bad=client.post('/login',data={'username':'guard-admin','password':ADMIN_PW,'backup_passphrase':'synthetic-archive-phrase-A','csrf_token':csrf(page)},follow_redirects=False)
        assert bad.status_code==401
        with factory() as db:
            assert db.get(SystemSetting,SETTING_KEY).value
            assert db.get(SystemSetting,'post_restore_passphrase_required').value
        page=client.get('/login')
        result=client.post('/login',data={'username':'guard-admin','password':ADMIN_PW,'backup_passphrase':'synthetic-recovery-phrase-B','csrf_token':csrf(page)},follow_redirects=False)
        assert result.status_code==303 and result.headers['location']=='/settings/password?forced=1'
        with factory() as db:
            assert db.get(SystemSetting,SETTING_KEY).value==''
            assert db.get(SystemSetting,'post_restore_passphrase_required').value==''
            assert db.get(SystemSetting,'post_restore_regen_required').value=='armed'
            assert db.scalar(select(AdminSession.id).where(AdminSession.id!='live-session-1'))

@pytest.mark.parametrize('corrupt',['epoch','json','record','key','fields'])
def test_corrupt_present_proof_never_falls_back_to_active_archive_phrase(factory,corrupt):
    prepare(factory)
    with factory.begin() as db:
        row=db.get(SystemSetting,SETTING_KEY);p=json.loads(row.value)
        if corrupt=='epoch':p['restore_epoch']='old'
        elif corrupt=='record':p['wrapped_record_sha256']='0'*64
        elif corrupt=='key':p['unwrapped_key_sha256']='0'*64
        elif corrupt=='fields':p['wrapped_record']['data_key']='0'*64
        row.value='bad-json' if corrupt=='json' else json.dumps(p)
    with factory() as db:
        with pytest.raises(ValueError):verify_login_phrase(db,'synthetic-archive-phrase-A')
        with pytest.raises(ValueError):verify_login_phrase(db,'synthetic-recovery-phrase-B')


def test_legacy_absent_proof_and_verification_does_not_consume(factory):
    prepare(factory)
    with factory() as db:
        before=db.get(SystemSetting,SETTING_KEY).value
        verify_login_phrase(db,'synthetic-recovery-phrase-B')
        assert db.get(SystemSetting,SETTING_KEY).value==before
        db.get(SystemSetting,SETTING_KEY).value='';db.flush()
        verify_login_phrase(db,'synthetic-archive-phrase-A')



def test_recovery_code_keeps_proof_until_complete_success(factory):
    from app.models import RecoveryCode,AdminAccount
    from app.security import hash_password
    from app.main import _recovery_lookup
    prepare(factory)
    code='SYNTHETIC-RECOVERY-CODE'
    with factory.begin() as db:
        db.add(RecoveryCode(admin_account_id=1,code_hash=hash_password(code),code_lookup=_recovery_lookup(db,code)))
    with TestClient(app) as client:
        page=client.get('/login/recover')
        bad=client.post('/login/recover',data={'username':'guard-admin','recovery_code':'wrong','new_password':ADMIN_PW+'new','confirm_password':ADMIN_PW+'new','backup_passphrase':'synthetic-recovery-phrase-B','csrf_token':csrf(page)},follow_redirects=False)
        assert bad.status_code==401
        with factory() as db:
            assert db.get(SystemSetting,SETTING_KEY).value
            assert db.scalar(select(RecoveryCode.used_at)) is None
        page=client.get('/login/recover')
        good=client.post('/login/recover',data={'username':'guard-admin','recovery_code':code,'new_password':ADMIN_PW+'new','confirm_password':ADMIN_PW+'new','backup_passphrase':'synthetic-recovery-phrase-B','csrf_token':csrf(page)},follow_redirects=False)
        assert good.status_code==303 and good.headers['location']=='/login?recovered=1'
        with factory() as db:
            assert db.get(SystemSetting,SETTING_KEY).value==''
            assert db.get(SystemSetting,'post_restore_passphrase_required').value==''
            assert db.get(SystemSetting,'post_restore_regen_required').value=='armed'
            assert db.scalar(select(RecoveryCode.used_at)) is not None
            assert db.scalar(select(AdminSession.id).where(AdminSession.admin_account_id==1)) is None


def test_wrong_account_password_never_consumes_proof(factory):
    prepare(factory)
    with TestClient(app) as client:
        page=client.get('/login')
        response=client.post('/login',data={'username':'guard-admin','password':'wrong','backup_passphrase':'synthetic-recovery-phrase-B','csrf_token':csrf(page)},follow_redirects=False)
        assert response.status_code==401
    with factory() as db:assert db.get(SystemSetting,SETTING_KEY).value
