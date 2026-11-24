"""Real migrated DB tests, transactions always rolled back, synthetic identities."""
import json
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.db import SessionLocal
from app.department_workbook import ENTRY_SHEET, FIRST_EMPLOYEE_ROW
from app.department_workbook_store import persist_export, prepare_return
from app.models import AdminAccount, AdminSession, DepartmentWorkbookPreparation
from app.restore_lock import read_restore_epoch
from tests.test_department_workbook import make_sample


@pytest.fixture
def context():
    with SessionLocal() as db:
        try:
            account = AdminAccount(username='synthetic-workbook-store', password_hash='not-a-real-login',
                                   role='system_admin', active=True)
            db.add(account); db.flush()
            session = AdminSession(id='synthetic-workbook-store-session', admin_account_id=account.id)
            db.add(session); db.flush()
            auth = {'actor_id': account.id, 'session_id': session.id, 'session_version': account.session_version,
                        'expected_epoch': read_restore_epoch(db)}
            payload, manifest = make_sample()
            row = persist_export(db, manifest=manifest, payload=payload, source_digest='a'*64, **auth)
            yield db, account, session, auth, row, payload
        finally:
            db.rollback()


def count(db):
    return db.scalar(select(func.count()).select_from(DepartmentWorkbookPreparation))


def test_clean_prepare_replay_and_transaction_rollback(context):
    db, _account, _session, auth, row, payload = context
    before = count(db)
    with db.begin_nested() as tx:
        receipt = prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
        same = prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
        assert same.id == receipt.id
        evidence = json.loads(receipt.evidence_json)
        assert evidence and all(set(day) == {'employee_id', 'date', 'code', 'raw_value', 'sheet', 'cell'} for day in evidence)
        assert count(db) == before + 1
        tx.rollback()
    assert count(db) == before


@pytest.mark.parametrize('change', ['role', 'active', 'version', 'password', 'pending', 'session', 'source', 'invalidation', 'epoch'])
def test_current_authority_and_freshness_rechecked_even_replay(context, change):
    db, account, session, auth, row, payload = context
    receipt = prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
    before = count(db)
    source = 'a'*64
    if change == 'role': account.role = 'assistant_manager'
    elif change == 'active': account.active = False
    elif change == 'version': account.session_version += 1
    elif change == 'password': account.must_change_password = True
    elif change == 'pending': account.pending_credential_reset = True
    elif change == 'session': db.delete(session)
    elif change == 'source': source = 'b'*64
    elif change == 'invalidation': row.invalidated_at = receipt.created_at
    elif change == 'epoch': row.restore_epoch = 'old-synthetic-epoch'
    db.flush()
    with pytest.raises(ValueError):
        prepare_return(db, export_id=row.id, payload=payload, current_source_digest=source, **auth)
    assert count(db) == before


@pytest.mark.parametrize('value', ['INVALID', '=1+1'])
def test_invalid_day_has_no_partial_preparation(context, value):
    db, _account, _session, auth, row, payload = context
    wb = load_workbook(BytesIO(payload)); wb[ENTRY_SHEET].cell(FIRST_EMPLOYEE_ROW, 3).value = value
    buf = BytesIO(); wb.save(buf); wb.close()
    before = count(db)
    with pytest.raises(ValueError):
        prepare_return(db, export_id=row.id, payload=buf.getvalue(), current_source_digest='a'*64, **auth)
    assert count(db) == before


def test_tamper_and_unknown_locator_do_not_persist(context):
    db, _account, _session, auth, row, payload = context
    before = count(db)
    with pytest.raises(ValueError):
        prepare_return(db, export_id='x'*64, payload=payload, current_source_digest='a'*64, **auth)
    row.manifest_json = '{}'; db.flush()
    with pytest.raises(ValueError):
        prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
    assert count(db) == before


def test_second_authenticated_hr_session_not_exporter_locked(context):
    db, _account, _session, _auth, row, payload = context
    other = AdminAccount(username='synthetic-workbook-other', password_hash='not-real', role='system_admin', active=True)
    db.add(other); db.flush()
    session = AdminSession(id='synthetic-workbook-other-session', admin_account_id=other.id)
    db.add(session); db.flush()
    receipt = prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64,
        actor_id=other.id, session_id=session.id, session_version=other.session_version,
        expected_epoch=read_restore_epoch(db))
    assert receipt.actor_id == other.id
    assert row.actor_id != receipt.actor_id


def test_distinct_clean_return_preserves_both_preparations(context):
    db, _account, _session, auth, row, payload = context
    original = prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
    wb = load_workbook(BytesIO(payload)); wb[ENTRY_SHEET].cell(FIRST_EMPLOYEE_ROW, 3).value = 'P'
    buf = BytesIO(); wb.save(buf); wb.close()
    modified = prepare_return(db, export_id=row.id, payload=buf.getvalue(), current_source_digest='a'*64, **auth)
    assert original.id != modified.id
    assert json.loads(original.evidence_json)[0]['code'] is None
    assert json.loads(modified.evidence_json)[0]['code'] == 'P'


def test_global_immutable_change_refused_before_receipt(context):
    db, _account, _session, auth, row, payload = context
    wb = load_workbook(BytesIO(payload)); wb[ENTRY_SHEET].cell(FIRST_EMPLOYEE_ROW, 2).value = 'Changed name'
    buf = BytesIO(); wb.save(buf); wb.close()
    before = count(db)
    with pytest.raises(ValueError):
        prepare_return(db, export_id=row.id, payload=buf.getvalue(), current_source_digest='a'*64, **auth)
    assert count(db) == before


def test_mismatched_export_bytes_cannot_persist_binding(context):
    from app.models import DepartmentWorkbookExport
    db, _account, _session, auth, _row, payload = context
    _, manifest = make_sample()
    before = db.scalar(select(func.count()).select_from(DepartmentWorkbookExport))
    with pytest.raises(ValueError, match='Export bytes'):
        persist_export(db, manifest=manifest, payload=payload+b'changed', source_digest='a'*64, **auth)
    assert db.scalar(select(func.count()).select_from(DepartmentWorkbookExport)) == before


@pytest.mark.parametrize('role', ['assistant_manager', 'finance'])
def test_balance_bearing_export_hr_only(context, role):
    db, account, _session, auth, _row, payload = context
    _, manifest = make_sample()
    account.role = role
    db.flush()
    with pytest.raises(ValueError, match='authorized session'):
        persist_export(db, manifest=manifest, payload=payload, source_digest='a'*64, **auth)


def test_epoch_and_bad_container_refusals_are_value_errors(context):
    db, _account, _session, auth, row, _payload = context
    with pytest.raises(ValueError, match='epoch changed'):
        prepare_return(db, export_id=row.id, payload=b'notzip', current_source_digest='a'*64,
                       **{**auth, 'expected_epoch': 'wrong-synthetic-epoch'})
    before = count(db)
    with pytest.raises(ValueError, match='container'):
        prepare_return(db, export_id=row.id, payload=b'notzip', current_source_digest='a'*64, **auth)
    assert count(db) == before


def test_semantically_valid_manifest_rebind_refused_before_replay(context):
    db, _account, _session, auth, row, payload = context
    prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
    original = row.manifest_json
    manifest = json.loads(original)
    employees = manifest['manifest']['employees']
    employees[0]['employee_id'], employees[1]['employee_id'] = employees[1]['employee_id'], employees[0]['employee_id']
    row.manifest_json = json.dumps(manifest); db.flush()
    before = count(db)
    with pytest.raises(ValueError, match='integrity'):
        prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
    assert count(db) == before


def test_timeout_restored_on_success_and_domain_refusal(context):
    from sqlalchemy import text
    db, _account, _session, auth, row, payload = context
    db.execute(text("SET LOCAL lock_timeout='8s'"))
    prepare_return(db, export_id=row.id, payload=payload, current_source_digest='a'*64, **auth)
    assert db.scalar(text('SHOW lock_timeout')) == '8s'
    with pytest.raises(ValueError):
        prepare_return(db, export_id='notfound', payload=payload, current_source_digest='a'*64, **auth)
    assert db.scalar(text('SHOW lock_timeout')) == '8s'


def test_advisory_wait_honors_stricter_existing_timeout():
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from app.db import engine
    from app.restore_lock import RESTORE_LOCK_KEY
    with engine.connect() as blocker, SessionLocal() as db, blocker.begin():
        blocker.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': RESTORE_LOCK_KEY})
        db.execute(text("SET LOCAL lock_timeout='100ms'"))
        with pytest.raises(OperationalError, match='lock timeout'):
            prepare_return(db, export_id='missing', payload=b'', current_source_digest='a'*64,
                actor_id=1, session_id='synthetic', session_version=0, expected_epoch=None)
        db.rollback()
        assert db.scalar(text('SELECT 1')) == 1


def test_receipt_hash_replay_and_legacy_reparse_not_blind_backfill(context):
    db,_account,_session,auth,row,payload=context
    receipt=prepare_return(db,export_id=row.id,payload=payload,current_source_digest='a'*64,**auth)
    original=receipt.evidence_json
    assert receipt.evidence_sha256
    receipt.evidence_json+=' ';db.flush()
    with pytest.raises(ValueError,match='integrity'):
        prepare_return(db,export_id=row.id,payload=payload,current_source_digest='a'*64,**auth)
    receipt.evidence_sha256=None;db.flush()
    with pytest.raises(ValueError,match='Unverified'):
        prepare_return(db,export_id=row.id,payload=payload,current_source_digest='a'*64,**auth)
    receipt.evidence_json=original;db.flush()
    verified=prepare_return(db,export_id=row.id,payload=payload,current_source_digest='a'*64,**auth)
    assert verified.id==receipt.id and verified.evidence_sha256


@pytest.mark.parametrize('change',[{'employee_id':True},{'date':[]},{'code':[]},{'sheet':{}},{'cell':'A0'},{'raw_value':1}])
def test_hash_valid_malformed_receipt_rows_refuse_schema(context,change):
    from hashlib import sha256
    db,_account,_session,auth,row,payload=context
    receipt=prepare_return(db,export_id=row.id,payload=payload,current_source_digest='a'*64,**auth)
    rows=json.loads(receipt.evidence_json);rows[0].update(change)
    receipt.evidence_json=json.dumps(rows)
    receipt.evidence_sha256=sha256(receipt.evidence_json.encode()).hexdigest();db.flush()
    with pytest.raises(ValueError): prepare_return(db,export_id=row.id,payload=payload,current_source_digest='a'*64,**auth)
