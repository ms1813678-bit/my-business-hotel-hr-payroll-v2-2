"""Server-held review receipts for preparation edits, never leave approval."""
import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leave_register_preparation import save_reviewed_register_preparation
from app.leave_register_review import RegisterEdit, prepare_register_edit
from app.models import LeaveRegisterReviewReceipt, utcnow


def _hash(token):
    if not isinstance(token, str) or not token or len(token) > 200:
        raise ValueError('Invalid review receipt.')
    return hashlib.sha256(token.encode()).hexdigest()


def _edit(payload):
    data = json.loads(payload)
    for name in ('work_date', 'period_start', 'period_end'):
        data[name] = date.fromisoformat(data[name])
    return RegisterEdit(**data)


def issue_register_receipt(engine, *, edit, actor_id, session_id, session_version):
    token = secrets.token_urlsafe(32)
    with Session(engine) as db, db.begin():
        review = prepare_register_edit(db, edit)
        if review.issues:
            raise ValueError('Register review blocked: ' + ','.join(review.issues))
        # Receipt issuance conveys no mutation authority. Confirm independently
        # rechecks authenticated identity and the kernel checks live DB authority.
        db.add(LeaveRegisterReviewReceipt(token_hash=_hash(token), actor_id=actor_id,
            session_id=session_id, session_version=session_version,
            proposal_json=json.dumps(asdict(edit), default=str, ensure_ascii=True),
            snapshot_json=json.dumps(asdict(review), ensure_ascii=True),
            source_digest=review.source_digest, expires_at=utcnow() + timedelta(minutes=10)))
    return token, review


def inspect_register_receipt(engine, *, token, actor_id, session_id, session_version):
    with Session(engine) as db:
        row = db.get(LeaveRegisterReviewReceipt, _hash(token))
        _require(row, actor_id, session_id, session_version)
        return _edit(row.proposal_json), json.loads(row.snapshot_json), row.source_digest


def _require(row, actor_id, session_id, session_version):
    if (row is None or row.actor_id != actor_id or row.session_version != session_version
            or not hmac.compare_digest(row.session_id, session_id)
            or row.consumed_at is not None or row.expires_at <= utcnow()):
        raise ValueError('Review receipt expired, used or belongs to another session.')


def confirm_register_receipt(engine, *, token, actor_id, session_id, session_version,
                             human_confirmed):
    if human_confirmed is not True:
        raise ValueError('Explicit human confirmation required.')
    edit, _snapshot, digest = inspect_register_receipt(engine, token=token,
        actor_id=actor_id, session_id=session_id, session_version=session_version)
    held = {}

    def check(db):
        row = db.scalar(select(LeaveRegisterReviewReceipt).where(
            LeaveRegisterReviewReceipt.token_hash == _hash(token)).with_for_update())
        _require(row, actor_id, session_id, session_version)
        if row.proposal_json != json.dumps(asdict(edit), default=str, ensure_ascii=True):
            raise ValueError('Review receipt proposal changed.')
        if not hmac.compare_digest(row.source_digest, digest):
            raise ValueError('Review receipt digest changed.')
        held['row'] = row

    def consume(db):
        _require(held['row'], actor_id, session_id, session_version)
        held['row'].consumed_at = utcnow()
        db.flush()

    return save_reviewed_register_preparation(engine, edit=edit, actor_id=actor_id,
        session_id=session_id, session_version=session_version, reviewed_digest=digest,
        _review_check=check, _review_consume=consume)
