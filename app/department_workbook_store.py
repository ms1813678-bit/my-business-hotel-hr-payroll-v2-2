"""Unexposed workbook persistence foundation. Never posts attendance or balances.

No HTTP caller yet. Source digest must come from a future authoritative adapter
inside this transaction, not a browser field. Balance-bearing exports are system_admin (HR) only, per owner choice.
Other upload/review permissions are not decided by that export-only choice.
Preparation is repeatable evidence, NOT review approval, consumption or posting.
"""
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, date
from zipfile import BadZipFile

from sqlalchemy import select

from app.department_manifest import decode_manifest, encode_manifest
from app.department_workbook import inspect_department_return
from app.department_workbook_guard import bounded_workbook_operation
from app.models import (
    AdminAccount,
    AdminSession,
    DepartmentWorkbookExport,
    DepartmentWorkbookPreparation,
)
from app.restore_lock import acquire_credential_write_lock, read_restore_epoch



def decode_receipt_evidence(text):
    """Strict persisted row schema. Hash checks are separate, not authorization."""
    if not isinstance(text,str) or len(text.encode())>25000000:
        raise ValueError('Bounded receipt evidence required.')
    rows=json.loads(text)
    if type(rows) is not list or not 1<=len(rows)<=9300:
        raise ValueError('Bounded saved day review required.')
    for row in rows:
        if type(row) is not dict or set(row)!={'employee_id','date','code','raw_value','sheet','cell'}:
            raise ValueError('Exact saved day fields required.')
        if type(row['employee_id']) is not int or row['employee_id']<1:
            raise ValueError('Saved employee identity required.')
        if not isinstance(row['date'],str) or date.fromisoformat(row['date']).isoformat()!=row['date']:
            raise ValueError('Saved calendar date required.')
        if row['code'] is not None and (not isinstance(row['code'],str) or row['code'] not in {'P','A','W','H','S','U'}):
            raise ValueError('Saved day code required.')
        if row['raw_value'] is not None and not isinstance(row['raw_value'],str):
            raise ValueError('Saved original cell text required.')
        if not isinstance(row['sheet'],str) or not row['sheet'] or not isinstance(row['cell'],str) or re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]{0,6}',row['cell']) is None:
            raise ValueError('Saved original cell position required.')
    return rows


def _digest(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Exact source digest required.')
    return value


def _guard(db, *, actor_id, session_id, session_version, expected_epoch):
    acquire_credential_write_lock(db, expected_epoch=expected_epoch)
    # Always refresh ORM identity-map rows after the restore/credential gate.
    account = db.scalar(select(AdminAccount).where(AdminAccount.id == actor_id).execution_options(populate_existing=True))
    session = db.scalar(select(AdminSession).where(AdminSession.id == session_id).execution_options(populate_existing=True))
    if (account is None or session is None or session.admin_account_id != account.id
            or not account.active or account.must_change_password or account.pending_credential_reset
            or account.session_version != session_version or account.role != 'system_admin'):
        raise ValueError('Current authorized session required.')
    return account


@bounded_workbook_operation
def persist_export(db, *, manifest, payload, source_digest, actor_id, session_id,
                   session_version, expected_epoch):
    """Caller owns transaction, live source snapshot, and eventual delivery.

    No implicit commit. A random reference is a locator, never a bearer grant.
    No expiry or superseding-export policy is imposed here.
    """
    _guard(db, actor_id=actor_id, session_id=session_id, session_version=session_version,
           expected_epoch=expected_epoch)
    text = encode_manifest(manifest)
    if hashlib.sha256(payload).hexdigest() != manifest.export_sha256:
        raise ValueError('Export bytes do not match manifest.')
    preview = _inspect(payload, manifest)
    if preview.issues or any(day.issue for day in preview.days):
        raise ValueError('Export does not pass its own manifest checks.')
    row = DepartmentWorkbookExport(id=secrets.token_hex(32), actor_id=actor_id,
        manifest_json=text, manifest_sha256=hashlib.sha256(text.encode()).hexdigest(), source_digest=_digest(source_digest), restore_epoch=read_restore_epoch(db),
        created_at=datetime.now(UTC), invalidated_at=None)
    db.add(row)
    db.flush()
    return row


@bounded_workbook_operation
def prepare_return(db, *, export_id, payload, current_source_digest, actor_id,
                   session_id, session_version, expected_epoch):
    """Atomically persist clean day evidence only, never registration effects.

    Check current scope/source even for a replay. All day/global issues refuse
    before any preparation row. A distinct return is distinct preparation,
    not implicit replacement or a second posting. Caller commits/rolls back.
    """
    _guard(db, actor_id=actor_id, session_id=session_id, session_version=session_version,
           expected_epoch=expected_epoch)
    row = db.scalar(select(DepartmentWorkbookExport).where(DepartmentWorkbookExport.id == export_id)
                    .with_for_update().execution_options(populate_existing=True))
    if row is None or row.invalidated_at is not None:
        raise ValueError('Workbook export unavailable.')
    if row.restore_epoch != read_restore_epoch(db):
        raise ValueError('Workbook belongs to an earlier restore epoch.')
    if row.source_digest != _digest(current_source_digest):
        raise ValueError('Workbook source changed; regenerate and review.')
    if hashlib.sha256(row.manifest_json.encode()).hexdigest() != row.manifest_sha256:
        raise ValueError('Stored workbook manifest integrity mismatch.')
    manifest = decode_manifest(row.manifest_json)
    digest = hashlib.sha256(payload).hexdigest()
    existing = db.scalar(select(DepartmentWorkbookPreparation).where(
        DepartmentWorkbookPreparation.export_id == row.id,
        DepartmentWorkbookPreparation.return_sha256 == digest))
    if existing is not None and existing.evidence_sha256 is not None:
        if hashlib.sha256(existing.evidence_json.encode()).hexdigest()!=existing.evidence_sha256:
            raise ValueError('Stored returned evidence integrity mismatch.')
        decode_receipt_evidence(existing.evidence_json)
        return existing
    preview = _inspect(payload, manifest)
    if preview.issues or any(day.issue for day in preview.days):
        raise ValueError('Workbook has unresolved global or day issues.')
    evidence = [{'employee_id': day.employee_id, 'date': day.work_date.isoformat(),
                 'code': day.code, 'raw_value': day.raw_value,
                 'sheet': day.sheet, 'cell': day.cell} for day in preview.days]
    text=json.dumps(evidence,ensure_ascii=False,sort_keys=True)
    decode_receipt_evidence(text)
    if existing is not None:
        # A legacy unverified receipt is upgraded only by reparsing actual bytes
        # against the current verified manifest/source, never by hashing old JSON.
        if existing.evidence_json!=text:
            raise ValueError('Unverified receipt does not match original returned evidence.')
        existing.evidence_sha256=hashlib.sha256(text.encode()).hexdigest()
        db.flush()
        return existing
    prepared = DepartmentWorkbookPreparation(export_id=row.id, return_sha256=digest,
        actor_id=actor_id, evidence_json=text,evidence_sha256=hashlib.sha256(text.encode()).hexdigest(),
        created_at=datetime.now(UTC))
    db.add(prepared)
    db.flush()
    return prepared


def _inspect(payload, manifest):
    try:
        return inspect_department_return(payload, manifest)
    except BadZipFile as exc:
        raise ValueError('Invalid workbook container.') from exc
