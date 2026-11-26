"""Independent closeout of the localized replay fix in leave_opening_rebind.py.
Baseline: pre-fix packet run 19 passed (2 of my characterizations asserted the OLD
trusting-replay behavior). Post-fix expectations: exact same-op replay stays idempotent
with zero writes; changed reason, other actor and poisoned state refuse."""
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.leave_opening_apply import RECEIPT_ACTION, apply_confirmed_opening, review_opening_apply
from app.leave_opening_rebind import apply_opening_rebind
from app.models import AdminAccount, AdminSession, AuditLog, Employee, EmployeeServicePeriod, LeaveType

from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401

BAL = {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)}


@pytest.fixture
def pair(history_context):  # noqa: F811
    from tests.test_restore_epoch_rebind import confirm
    db, _actor, auth, fact = history_context
    e = db.get(Employee, fact.employee_id)
    e.hire_date = date(2026, 2, 25)
    db.add(EmployeeServicePeriod(employee_id=e.id, start_date=e.hire_date))
    selected = confirm(db, e, auth, BAL)
    kinds = {c: db.scalar(select(LeaveType.id).where(LeaveType.code == h)) for c, h in
             {'A': 'annual', 'W': 'weekly_rest', 'H': 'official_holiday'}.items()}
    opening = review_opening_apply(db, selection_id=selected.id, type_map=kinds, **auth)
    apply_confirmed_opening(db, selection_id=selected.id, type_map=kinds,
                            reviewed_apply_sha256=opening['apply_sha256'],
                            human_confirmed=True, reason='Synthetic opening', **auth)
    yield db, e, kinds, selected, auth


def _bound(pair):
    from tests.test_restore_epoch_rebind import confirm, restore, rebind
    db, e, kinds, old, auth = pair
    auth2 = restore(db, auth, 'E1-restored')
    new = confirm(db, e, auth2, BAL)
    review, receipt = rebind(db, old.id, new.id, auth2)
    assert receipt['replayed'] is False
    return db, old, new, auth2, review, receipt


def _audits(db):
    return db.scalar(select(func.count()).select_from(AuditLog))


def test_legit_exact_same_op_replay_still_idempotent_no_writes(pair):
    """The fix must not break the legitimate same operation: same actor, session, reason,
    digest and unchanged state -> replayed True, zero additional writes, same receipt."""
    db, old, new, auth2, review, receipt = _bound(pair)
    before = _audits(db)
    again = apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
                                 reviewed_rebind_sha256=review['rebind_sha256'],
                                 reason='Synthetic rebind', human_confirmed=True, **auth2)
    assert again['replayed'] is True
    assert again['rebind_audit_id'] == receipt['rebind_audit_id']
    assert again['rebind_sha256'] == receipt['rebind_sha256']
    assert _audits(db) == before


def test_changed_reason_replay_refuses(pair):
    db, old, new, auth2, review, receipt = _bound(pair)
    before = _audits(db)
    with pytest.raises(ValueError):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
                             reviewed_rebind_sha256=review['rebind_sha256'],
                             reason='Synthetic rebind CHANGED', human_confirmed=True, **auth2)
    assert _audits(db) == before


def test_other_actor_replay_refuses(pair):
    db, old, new, auth2, review, receipt = _bound(pair)
    acc2 = AdminAccount(username='synthetic-second-hr', password_hash='not-a-real-login',
                        role='system_admin', active=True)
    db.add(acc2)
    db.flush()
    sess2 = AdminSession(id='synthetic-second-session-e1', admin_account_id=acc2.id)
    db.add(sess2)
    db.flush()
    auth2b = {'actor_id': acc2.id, 'session_id': sess2.id,
              'session_version': acc2.session_version, 'expected_epoch': 'E1-restored'}
    before = _audits(db)
    with pytest.raises(ValueError):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
                             reviewed_rebind_sha256=review['rebind_sha256'],
                             reason='Synthetic rebind', human_confirmed=True, **auth2b)
    assert _audits(db) == before


def test_poisoned_dual_apply_replay_now_refuses(pair):
    """Original flagged case, inverted: poisoned dual-receipt state must now refuse the
    replay (fresh plan runs first), not return stored success."""
    db, old, new, auth2, review, receipt = _bound(pair)
    old_receipt = db.scalar(select(AuditLog).where(AuditLog.action == RECEIPT_ACTION,
                                                   AuditLog.entity_id == old.id))
    db.add(AuditLog(actor='forged', action=RECEIPT_ACTION, entity_type=old_receipt.entity_type,
                    entity_id=new.id, old_value='', new_value=old_receipt.new_value,
                    reason='forged second apply receipt'))
    db.flush()
    db.expire_all()
    before = _audits(db)
    with pytest.raises(ValueError):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
                             reviewed_rebind_sha256=review['rebind_sha256'],
                             reason='Synthetic rebind', human_confirmed=True, **auth2)
    assert _audits(db) == before
