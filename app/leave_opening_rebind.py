"""Fresh-proof rebind of a confirmed manual opening after an owner-approved restore.

A restore changes the epoch, so every epoch-bound source row of the old chain is
unreadable by design. This module never edits or copies old rows, never posts a
ledger row and never grants anything. A reviewed HR session in the CURRENT epoch
re-confirms the same source (new group/policy/selection rows) and then writes one
immutable audit receipt that names the old selection. Verifiers follow the lineage
back to the original apply receipt, so the original ledger remains the only credit
and a second opening stays refused.
"""
import json
import re
from hashlib import sha256

from sqlalchemy import select

from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_opening_apply import RECEIPT_ACTION
from app.models import AuditLog, LeaveOpeningSelection

ACTION = 'leave_opening_rebound'
ENTITY = 'leave_opening_selection'
HEX64 = re.compile('^[0-9a-f]{64}$')
MAX_DEPTH = 16
BODY_KEYS = {'old_selection_id', 'old_epoch', 'new_epoch', 'employee_id', 'amounts', 'effective_start',
             'old_apply_sha256', 'old_source_sha256', 'new_source_sha256', 'policy_sha256'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _fail(message='Opening rebind receipt does not verify.'):
    raise ValueError(message)


def _parse_body(row, new_id):
    """Strict parse. Malformed stored proof refuses; it is never ignored."""
    try:
        body = json.loads(row.new_value)
    except (TypeError, ValueError):
        _fail('Malformed opening rebind receipt; refusing, not ignored.')
    if (type(body) is not dict or set(body) != BODY_KEYS or row.old_value != body['old_selection_id']
            or row.entity_id != new_id
            or not all(isinstance(body[k], str) and HEX64.match(body[k]) for k in
                       ('old_selection_id', 'old_apply_sha256', 'old_source_sha256', 'new_source_sha256', 'policy_sha256'))
            or type(body['employee_id']) is not int or type(body['amounts']) is not dict
            or set(body['amounts']) != {'A', 'W', 'H'} or not all(isinstance(v, str) for v in body['amounts'].values())
            or not isinstance(body['effective_start'], str)
            or not (body['old_epoch'] is None or isinstance(body['old_epoch'], str))
            or not isinstance(body['new_epoch'], str) or body['old_epoch'] == body['new_epoch']):
        _fail('Malformed opening rebind receipt; refusing, not ignored.')
    return body


def rebind_row(db, selection_id):
    rows = db.scalars(select(AuditLog).where(AuditLog.action == ACTION, AuditLog.entity_type == ENTITY,
        AuditLog.entity_id == selection_id).limit(2).execution_options(populate_existing=True)).all()
    if len(rows) > 1:
        _fail('Several opening rebind receipts for one selection; refusing.')
    return rows[0] if rows else None


def has_apply_receipt(db, selection_id):
    return db.scalar(select(AuditLog.id).where(AuditLog.action == RECEIPT_ACTION,
        AuditLog.entity_type == ENTITY, AuditLog.entity_id == selection_id).limit(1)) is not None


def lineage(db, selection_id):
    """[selection_id, predecessor, ..., root]; root carries the original apply receipt.

    Each link is a rebind receipt. Raises ValueError for a missing, malformed or cyclic chain.
    """
    chain, bodies = [selection_id], []
    while True:
        cur = chain[-1]
        if has_apply_receipt(db, cur):
            if bodies and rebind_row(db, cur) is not None:
                _fail('A rebound selection cannot also carry an apply receipt.')
            return chain, bodies
        row = rebind_row(db, cur)
        if row is None or len(chain) >= MAX_DEPTH:
            _fail('Opening selection has no apply receipt or verified rebind lineage.')
        body = _parse_body(row, cur)
        if body['old_selection_id'] in chain:
            _fail('Cyclic opening rebind lineage.')
        chain.append(body['old_selection_id'])
        bodies.append(body)


def lineage_ids(db, selection_id):
    return set(lineage(db, selection_id)[0])


def current_descendant(db, selection_id, epoch):
    """The selection in `epoch` that was rebound from `selection_id` (itself when already current)."""
    cur = selection_id
    for _ in range(MAX_DEPTH):
        row = db.get(LeaveOpeningSelection, cur, populate_existing=True)
        if row is None:
            return None
        if row.restore_epoch == epoch:
            return cur
        succ = db.scalars(select(AuditLog).where(AuditLog.action == ACTION, AuditLog.entity_type == ENTITY,
            AuditLog.old_value == cur).limit(2)).all()
        if len(succ) != 1:
            return None
        cur = succ[0].entity_id
    return None


def _amounts(reviews):
    return {r['code']: r['days'] for r in reviews}


def _plan(db, *, old_selection_id, new_selection_id, identity):
    from decimal import Decimal

    from app.leave_manual_opening_groups import _read_verified_manual_group  # noqa: F401
    from app.leave_opening_receipt import _verify_apply_core, verify_rebind_chain
    from app.leave_opening_source import _current, _read_verified_opening_source, read_confirmed_opening_source
    from app.leave_policy_store import canonical_policy

    epoch = identity['expected_epoch']
    if (not isinstance(old_selection_id, str) or not HEX64.match(old_selection_id)
            or not isinstance(new_selection_id, str) or not HEX64.match(new_selection_id)
            or old_selection_id == new_selection_id):
        _fail('Two distinct confirmed selection ids required.')
    old = db.get(LeaveOpeningSelection, old_selection_id, populate_existing=True)
    new = db.get(LeaveOpeningSelection, new_selection_id, populate_existing=True)
    if (old is None or new is None or old.employee_id != new.employee_id
            or new.restore_epoch != epoch or old.restore_epoch == epoch):
        _fail('Old-epoch selection and current-epoch selection of one employee required.')
    if has_apply_receipt(db, new_selection_id):
        _fail('Current selection already carries an apply receipt; not a rebind.')
    other = db.scalar(select(AuditLog.id).where(AuditLog.action == ACTION, AuditLog.entity_type == ENTITY,
        AuditLog.old_value == old_selection_id, AuditLog.entity_id != new_selection_id).limit(1))
    if other is not None:
        _fail('Old selection is already rebound to another selection.')
    head = _current(db, old.employee_id, old.restore_epoch)
    if head is None or head.id != old.id:
        _fail('Old selection was replaced in its own epoch; not the applied head.')
    # Old chain proof: original apply receipt + ledger + audit, then every earlier rebind.
    chain, bodies = lineage(db, old_selection_id)
    root_core = _verify_apply_core(db, chain[-1])
    verify_rebind_chain(db, chain, bodies, root_core, final_current=None)
    # Old source, read under its OWN epoch, must still be intact (immutable archive rows).
    old_reviews, old_policy, old_digest = _read_verified_opening_source(
        db, selection_id=old_selection_id, expected_epoch=old.restore_epoch)
    new_reviews, new_policy, new_digest = read_confirmed_opening_source(db, selection_id=new_selection_id, **identity)
    old_amounts, new_amounts = _amounts(old_reviews), _amounts(new_reviews)
    if (old_amounts != new_amounts or {r['effective_start'] for r in old_reviews} != {r['effective_start'] for r in new_reviews}
            or len({r['effective_start'] for r in new_reviews}) != 1
            or {r['employee_id'] for r in new_reviews} != {old.employee_id}
            or {r['employee_id'] for r in old_reviews} != {old.employee_id}):
        _fail('Current source must equal the archived opening amounts, basis date and employee.')
    if canonical_policy(old_policy) != canonical_policy(new_policy):
        _fail('Policy differs from the archived opening; a reviewed correction is required, not a rebind.')
    if {c: Decimal(v) for c, v in old_amounts.items()} != root_core['amounts']:
        _fail('Archived opening amounts do not match the applied receipt.')
    body = {'old_selection_id': old_selection_id, 'old_epoch': old.restore_epoch, 'new_epoch': epoch,
            'employee_id': old.employee_id, 'amounts': old_amounts,
            'effective_start': new_reviews[0]['effective_start'],
            'old_apply_sha256': root_core['apply_sha256'], 'old_source_sha256': old_digest,
            'new_source_sha256': new_digest, 'policy_sha256': sha256(canonical_policy(new_policy).encode()).hexdigest()}
    return body, sha256(_canonical({'new_selection_id': new_selection_id, **body}).encode()).hexdigest()


@bounded_workbook_operation
def review_opening_rebind(db, *, old_selection_id, new_selection_id, **identity):
    """Read-only. Shows exactly what a rebind would bind; posts nothing."""
    from app.leave_policy_store import policy_read_guard
    policy_read_guard(db, **identity)
    body, digest = _plan(db, old_selection_id=old_selection_id, new_selection_id=new_selection_id, identity=identity)
    return {'rebind_sha256': digest, 'plan': {'new_selection_id': new_selection_id, **body}, 'ledger_applied': False}


@bounded_workbook_operation
def apply_opening_rebind(db, *, old_selection_id, new_selection_id, reviewed_rebind_sha256, reason,
                         human_confirmed, **identity):
    """Write one immutable rebind receipt. No ledger, balance, policy or credential change."""
    account = _guard(db, **identity)
    if (human_confirmed is not True or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000
            or not isinstance(reviewed_rebind_sha256, str) or not HEX64.match(reviewed_rebind_sha256)):
        raise ValueError('Explicit human confirmation, reviewed digest and bounded reason required.')
    existing = rebind_row(db, new_selection_id) if isinstance(new_selection_id, str) else None
    body, digest = _plan(db, old_selection_id=old_selection_id, new_selection_id=new_selection_id, identity=identity)
    if existing is not None:
        stored = _parse_body(existing, new_selection_id)
        replay = sha256(_canonical({'new_selection_id': new_selection_id, **stored}).encode()).hexdigest()
        if existing.actor != account.username or existing.reason != reason.strip():
            raise ValueError('Rebind retry must use the original HR actor and unchanged reason.')
        if (stored['old_selection_id'] != old_selection_id or replay != reviewed_rebind_sha256
                or replay != digest or stored != body):
            raise ValueError('Selection was already rebound with different reviewed evidence.')
        from app.leave_opening_receipt import verified_manual_opening
        verified_manual_opening(db,selection_id=new_selection_id,**identity)
        return {'rebind_audit_id': existing.id, 'rebind_sha256': replay, 'replayed': True}
    if digest != reviewed_rebind_sha256:
        raise ValueError('Reviewed rebind evidence is stale; review again.')
    row = AuditLog(actor=account.username, action=ACTION, entity_type=ENTITY, entity_id=new_selection_id,
                   old_value=old_selection_id, new_value=_canonical(body), reason=reason.strip())
    db.add(row)
    db.flush()
    return {'rebind_audit_id': row.id, 'rebind_sha256': digest, 'replayed': False}
