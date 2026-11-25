"""One-use disaster-login possession proof. Does not rotate backup encryption."""
import hashlib
import hmac
import json
import re
from sqlalchemy import select
from app.models import SystemSetting
from app.restore_lock import read_restore_epoch
from app.backup_crypto import (unwrap_data_key_from_record, unwrap_data_key_for_restore,
                              SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_LEN)

SETTING_KEY = 'post_restore_login_proof'
PUBLIC_FIELDS = {'version','kdf','kdf_n','kdf_r','kdf_p','kdf_salt','wrap_nonce','wrapped_key'}


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'))


def checked_public_record(record):
    if type(record) is not dict:
        raise ValueError('Invalid recovery login record')
    public={k:record[k] for k in PUBLIC_FIELDS if k in record}
    if set(public)!=PUBLIC_FIELDS or public['version']!=1 or public['kdf']!='scrypt' or (public['kdf_n'],public['kdf_r'],public['kdf_p'])!=(SCRYPT_N,SCRYPT_R,SCRYPT_P):
        raise ValueError('Unsupported recovery login record')
    for name,length in [('kdf_salt',16),('wrap_nonce',12),('wrapped_key',48)]:
        if type(public[name]) is not str or not re.fullmatch('[0-9a-f]{'+str(length*2)+'}',public[name]):
            raise ValueError('Invalid recovery login record encoding')
    return public


def hash_value(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def clear_proof(db, *, actor):
    row=db.get(SystemSetting,SETTING_KEY)
    if row is not None:
        row.value=''
        row.updated_by=actor


def install_proof(db, *, record, data_key, archive_sha256, restore_epoch, actor):
    public=checked_public_record(record)
    if type(data_key) is not bytes or len(data_key)!=KEY_LEN or not isinstance(restore_epoch,str) or not restore_epoch or not re.fullmatch('[0-9a-f]{64}',archive_sha256):
        raise ValueError('Bound recovery login material required')
    payload=dict(version=1,restore_epoch=restore_epoch,archive_sha256=archive_sha256,
                 wrapped_record=public,wrapped_record_sha256=hash_value(public),
                 unwrapped_key_sha256=hashlib.sha256(data_key).hexdigest())
    db.merge(SystemSetting(key=SETTING_KEY,value=canonical(payload),updated_by=actor))


def verify_login_phrase(db, phrase):
    """Caller owns existing authorization/throttle lock and success transaction.

    Verification alone never consumes proof or removes any login restriction.
    A present corrupt proof refuses rather than falling back to the DB record.
    """
    row=db.scalar(select(SystemSetting).where(SystemSetting.key==SETTING_KEY).execution_options(populate_existing=True))
    if row is None or not row.value:
        return unwrap_data_key_for_restore(db,phrase)
    if len(row.value)>8192:
        raise ValueError('Invalid recovery login proof')
    payload=json.loads(row.value)
    if type(payload) is not dict or set(payload)!={'version','restore_epoch','archive_sha256','wrapped_record','wrapped_record_sha256','unwrapped_key_sha256'} or payload['version']!=1 or payload['restore_epoch']!=read_restore_epoch(db):
        raise ValueError('Stale or invalid recovery login proof')
    public=checked_public_record(payload['wrapped_record'])
    if public!=payload['wrapped_record'] or not hmac.compare_digest(hash_value(public),str(payload['wrapped_record_sha256'])):
        raise ValueError('Recovery login record binding mismatch')
    for field in ('archive_sha256','unwrapped_key_sha256'):
        if not isinstance(payload[field],str) or not re.fullmatch('[0-9a-f]{64}',payload[field]):
            raise ValueError('Invalid recovery login proof digest')
    key=unwrap_data_key_from_record(public,phrase)
    if len(key)!=KEY_LEN or not hmac.compare_digest(hashlib.sha256(key).hexdigest(),payload['unwrapped_key_sha256']):
        raise ValueError('Recovery login key binding mismatch')
    return key
