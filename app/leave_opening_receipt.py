"""Read-only verification of an immutable manual-opening apply receipt.

Recognition only: it never posts, never changes a balance, never treats the opening as
released credit and has no earned metadata. A selection verifies only when the stored audit
receipt, the ledger rows and the per-category audit rows all agree exactly; any difference raises
ValueError. When identity is supplied the receipt must also match the currently selected,
current-epoch confirmed source.
"""
import json
import re
from decimal import Decimal

from sqlalchemy import select

from app.leave_opening_apply import CANONICAL_TYPES, CODES, RECEIPT_ACTION
from app.models import AuditLog, LeaveBalanceTransaction, LeaveType

KEY = re.compile(r'^manual_opening:([0-9a-f]{64}):([AWH])$')
IDENTITY_KEYS = ('actor_id', 'session_id', 'session_version', 'expected_epoch')
BODY_KEYS = {'apply_sha256', 'amounts', 'type_ids', 'employee_id', 'source_sha256', 'ledger_ids'}
HEX64 = re.compile('^[0-9a-f]{64}$')


def _fail():
    raise ValueError('Manual opening receipt does not verify against ledger and audit evidence.')


def _verify_apply_core(db, selection_id):
    """Epoch-free proof of the ORIGINAL apply: audit receipt + ledger rows + per-category audits.

    Does not read the source rows. The caller binds the source (current-epoch read, or the
    rebind lineage). Returns the verified facts.
    """
    if not isinstance(selection_id, str) or not HEX64.match(selection_id):
        _fail()
    with db.no_autoflush:
        receipts = db.scalars(select(AuditLog).where(AuditLog.action == RECEIPT_ACTION,
            AuditLog.entity_type == 'leave_opening_selection', AuditLog.entity_id == selection_id)
            .limit(2).execution_options(populate_existing=True)).all()
        if len(receipts) != 1:
            _fail()
        receipt = receipts[0]
        try:
            body = json.loads(receipt.new_value)
        except (TypeError, ValueError):
            _fail()
        if (type(body) is not dict or set(body) != BODY_KEYS or not isinstance(body['apply_sha256'], str)
                or not HEX64.match(body['apply_sha256']) or not isinstance(body['source_sha256'], str)
                or type(body['employee_id']) is not int or type(body['amounts']) is not dict
                or set(body['amounts']) != set(CODES) or type(body['type_ids']) is not dict
                or set(body['type_ids']) != set(CODES) or type(body['ledger_ids']) is not list
                or any(type(i) is not int for i in body['ledger_ids'])
                or len(set(body['ledger_ids'])) != len(body['ledger_ids'])):
            _fail()
        canonical = {}
        for code in CODES:
            row = db.execute(select(LeaveType.id).where(LeaveType.code == CANONICAL_TYPES[code])).all()
            if len(row) != 1:
                _fail()
            canonical[code] = row[0].id
        if body['type_ids'] != canonical:
            _fail()
        amounts = {}
        for code in CODES:
            try:
                amount = Decimal(body['amounts'][code])
            except Exception:
                _fail()
            if not amount.is_finite() or amount < 0:
                _fail()
            amounts[code] = amount
        nonzero = {c for c in CODES if amounts[c] != 0}
        entries = db.scalars(select(LeaveBalanceTransaction).where(
            LeaveBalanceTransaction.source_operation_key.like('manual_opening:' + selection_id + ':%'))
            .order_by(LeaveBalanceTransaction.id).execution_options(populate_existing=True)).all()
        by_code = {}
        for entry in entries:
            match = KEY.match(entry.source_operation_key or '')
            if match is None or match.group(1) != selection_id or match.group(2) in by_code:
                _fail()
            code = match.group(2)
            if (code not in nonzero or entry.employee_id != body['employee_id']
                    or entry.leave_type_id != canonical[code] or entry.transaction_type != 'opening'
                    or entry.days_delta != amounts[code] or entry.source_digest != body['apply_sha256']
                    or entry.earning_year is not None or entry.released_on is not None):
                _fail()
            by_code[code] = entry.id
        if set(by_code) != nonzero or sorted(by_code.values()) != sorted(body['ledger_ids']):
            _fail()
        # One apply audit row per nonzero category and none for a zero category.
        audits = db.scalars(select(AuditLog).where(AuditLog.action == 'leave_balance_opening',
            AuditLog.entity_type == 'employee_leave_balance', AuditLog.reason == receipt.reason,
            AuditLog.entity_id.in_([f"{body['employee_id']}:{canonical[c]}" for c in CODES]))).all()
        expected = {f"{body['employee_id']}:{canonical[c]}": amounts[c] for c in nonzero}
        got = {}
        for audit in audits:
            if audit.entity_id in got or audit.entity_id not in expected or audit.old_value != '0':
                _fail()
            try:
                audit_amount = Decimal(audit.new_value)
            except Exception:
                _fail()
            if audit_amount != expected[audit.entity_id]:
                _fail()
            got[audit.entity_id] = True
        if set(got) != set(expected):
            _fail()
    return {'selection_id': selection_id, 'employee_id': body['employee_id'], 'source_sha256': body['source_sha256'],
            'apply_sha256': body['apply_sha256'], 'ledger_codes': {v: k for k, v in by_code.items()},
            'amounts': amounts}


def verify_rebind_chain(db, chain, bodies, root_core, *, final_current):
    """Each rebind keeps employee, amounts and original apply digest; epochs link in order.

    final_current: (reviews, digest, epoch) of the head selection read in the current epoch, or None
    when only the archived links are checked (used while planning a new rebind).
    """
    from app.models import LeaveOpeningSelection
    for index, body in enumerate(bodies):
        sel = db.get(LeaveOpeningSelection, chain[index], populate_existing=True)
        old = db.get(LeaveOpeningSelection, body['old_selection_id'], populate_existing=True)
        if (sel is None or old is None or sel.restore_epoch != body['new_epoch'] or old.restore_epoch != body['old_epoch']
                or sel.employee_id != root_core['employee_id'] or old.employee_id != root_core['employee_id']
                or body['employee_id'] != root_core['employee_id']
                or body['old_apply_sha256'] != root_core['apply_sha256']
                or {c: Decimal(v) for c, v in body['amounts'].items()} != root_core['amounts']
                or (index == len(bodies) - 1 and body['old_selection_id'] != root_core['selection_id'])
                or (index > 0 and bodies[index - 1]['old_epoch'] != body['new_epoch'])):
            _fail()
    if final_current is not None and bodies:
        reviews, digest = final_current
        head = bodies[0]
        if (digest != head['new_source_sha256'] or {r['employee_id'] for r in reviews} != {root_core['employee_id']}
                or {r['code']: Decimal(r['days']) for r in reviews} != root_core['amounts']
                or {r['effective_start'] for r in reviews} != {head['effective_start']}):
            _fail()


def verified_manual_opening(db, *, selection_id, **identity):
    """Return {'selection_id','employee_id','apply_sha256','ledger_codes':{id: code},'amounts'} or raise.

    A current-epoch selection verifies either directly (it carries the apply receipt) or through an
    immutable rebind lineage back to the original apply receipt; the original ledger is the only credit.
    """
    if not isinstance(selection_id, str) or not HEX64.match(selection_id) or set(identity) != set(IDENTITY_KEYS):
        _fail()
    from app.leave_opening_rebind import lineage
    from app.leave_opening_source import read_confirmed_opening_source
    try:
        chain, bodies = lineage(db, selection_id)
    except ValueError:
        _fail()
    core = _verify_apply_core(db, chain[-1])
    reviews, _policy, digest = read_confirmed_opening_source(db, selection_id=selection_id, **identity)
    if not bodies:
        if (digest != core['source_sha256'] or {r['employee_id'] for r in reviews} != {core['employee_id']}
                or {r['code']: Decimal(r['days']) for r in reviews} != core['amounts']):
            _fail()
    else:
        verify_rebind_chain(db, chain, bodies, core, final_current=(reviews, digest))
    return {'selection_id': selection_id, 'root_selection_id': chain[-1], 'employee_id': core['employee_id'],
            'apply_sha256': core['apply_sha256'], 'ledger_codes': core['ledger_codes'], 'amounts': core['amounts']}


def verified_receipt_ledger_ids(db, *, ledger_rows, selection_ids, **identity):
    """Split ledger_rows ({id: source_operation_key}) into (bound, unverified).

    bound: rows of a verified current selection. unverified: any other row whose key claims to be a
    manual opening (other selection, failed verification, malformed key). Full identity is mandatory.
    """
    if set(identity) != set(IDENTITY_KEYS):
        _fail()
    wanted = set(selection_ids)
    bound, unverified, verified = set(), set(), {}
    for ledger_id, key in ledger_rows.items():
        if not isinstance(key, str) or not key.startswith('manual_opening:'):
            continue
        match = KEY.match(key)
        if match is None or match.group(1) not in wanted:
            unverified.add(ledger_id)
            continue
        selection = match.group(1)
        if selection not in verified:
            try:
                # After a restore the key still names the ORIGINAL applied selection; verify the
                # current-epoch selection rebound from it (itself when it is already current).
                from app.leave_opening_rebind import current_descendant
                head = current_descendant(db, selection, identity['expected_epoch'])
                verified[selection] = None if head is None else verified_manual_opening(db, selection_id=head, **identity)
            except ValueError:
                verified[selection] = None
        receipt = verified[selection]
        if receipt is not None and ledger_id in receipt['ledger_codes']:
            bound.add(ledger_id)
        else:
            unverified.add(ledger_id)
    return bound, unverified
