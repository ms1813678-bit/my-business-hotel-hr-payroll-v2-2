"""Independent adversarial fixtures, integrated replay-refusal regression on current candidate."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.leave_opening_apply import RECEIPT_ACTION, apply_confirmed_opening, review_opening_apply
from app.leave_opening_rebind import _canonical, apply_opening_rebind, lineage, review_opening_rebind
from app.leave_opening_receipt import verified_manual_opening
from app.models import (AdminAccount, AdminSession, AuditLog, Employee, EmployeeServicePeriod,
                        LeaveHistoricalEvidencePreparation, LeaveType)

from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401

BAL = {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)}


@pytest.fixture
def pair(history_context):  # noqa: F811
    """An employee with a NONZERO applied opening under the pre-restore epoch."""
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


def _restored(db, e, auth):
    from tests.test_restore_epoch_rebind import confirm, restore
    auth2 = restore(db, auth, 'E1-restored')
    new = confirm(db, e, auth2, BAL)
    return new, auth2


def _audit_count(db):
    return db.scalar(select(func.count()).select_from(AuditLog))


def test_replay_rechecks_current_plan_after_dual_receipt_poison(pair):
    """Poisoned dual-receipt state refuses replay without another write."""
    from tests.test_restore_epoch_rebind import rebind
    db, e, kinds, old, auth = pair
    new, auth2 = _restored(db, e, auth)
    review, receipt = rebind(db, old.id, new.id, auth2)
    assert receipt['replayed'] is False
    before = _audit_count(db)
    old_receipt = db.scalar(select(AuditLog).where(AuditLog.action == RECEIPT_ACTION,
                                                   AuditLog.entity_id == old.id))
    # forged second apply receipt on the rebound selection (direct audit-row insert)
    db.add(AuditLog(actor='forged', action=RECEIPT_ACTION, entity_type=old_receipt.entity_type,
                    entity_id=new.id, old_value='', new_value=old_receipt.new_value,
                    reason='forged second apply receipt'))
    db.flush()
    db.expire_all()
    # a fresh rebind plan now refuses (new selection already carries an apply receipt)
    with pytest.raises(ValueError):
        review_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, **auth2)
    # but the replay branch returns success from the stored receipt WITHOUT re-planning
    with pytest.raises(ValueError):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
            reviewed_rebind_sha256=review['rebind_sha256'], reason='Synthetic rebind',human_confirmed=True,**auth2)
    assert _audit_count(db) == before + 1  # only the forged row; replay itself writes nothing
    # read path still fails closed on the poisoned dual-receipt state
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=new.id, **auth2)


def test_replay_by_second_actor_writes_nothing(pair):
    """A repeat apply from a DIFFERENT HR actor returns replayed and records the second
    actor nowhere: no new audit row, receipt still names only the first actor."""
    from tests.test_restore_epoch_rebind import rebind
    db, e, kinds, old, auth = pair
    new, auth2 = _restored(db, e, auth)
    review, receipt = rebind(db, old.id, new.id, auth2)
    acc2 = AdminAccount(username='synthetic-second-hr', password_hash='not-a-real-login',
                        role='system_admin', active=True)
    db.add(acc2)
    db.flush()
    sess2 = AdminSession(id='synthetic-second-session-e1', admin_account_id=acc2.id)
    db.add(sess2)
    db.flush()
    auth2b = {'actor_id': acc2.id, 'session_id': sess2.id,
              'session_version': acc2.session_version, 'expected_epoch': 'E1-restored'}
    before = _audit_count(db)
    with pytest.raises(ValueError):
        apply_opening_rebind(db,old_selection_id=old.id,new_selection_id=new.id,
            reviewed_rebind_sha256=review['rebind_sha256'],reason='Synthetic rebind',human_confirmed=True,**auth2b)
    assert _audit_count(db) == before
    row = db.scalar(select(AuditLog).where(AuditLog.action == 'leave_opening_rebound'))
    assert row.actor != 'synthetic-second-hr'


def test_forged_rebind_receipt_alone_establishes_lineage(pair):
    """A structurally valid rebind receipt row, inserted directly (no review, no guard,
    actor that never existed), is trusted by verification: the audit table is the trust
    root and the guarded apply path is the only barrier."""
    db, e, kinds, old, auth = pair
    new, auth2 = _restored(db, e, auth)
    review = review_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, **auth2)
    plan = dict(review['plan'])
    plan.pop('new_selection_id')
    db.add(AuditLog(actor='forged-no-review', action='leave_opening_rebound',
                    entity_type='leave_opening_selection', entity_id=new.id,
                    old_value=old.id, new_value=_canonical(plan), reason='forged'))
    db.flush()
    db.expire_all()
    v = verified_manual_opening(db, selection_id=new.id, **auth2)
    assert v['root_selection_id'] == old.id
    assert lineage(db, new.id)[0] == [new.id, old.id]


def test_proposal_epoch_index_null_and_empty_collide(pair):
    """0080 unique index: same proposal_sha256 is allowed across DISTINCT epochs, but a
    legacy NULL epoch and an empty-string epoch are ONE bucket (coalesce) and collide."""
    db, e, kinds, old, auth = pair
    base = dict(actor_id=auth['actor_id'], source_sha256='a' * 64, proposal_json='{}',
                proposal_sha256='b' * 64, created_at=datetime.now(timezone.utc))
    db.add(LeaveHistoricalEvidencePreparation(id='1' * 64, restore_epoch='E1', **base))
    db.add(LeaveHistoricalEvidencePreparation(id='2' * 64, restore_epoch='E2', **base))
    db.flush()  # distinct epochs coexist
    db.add(LeaveHistoricalEvidencePreparation(id='3' * 64, restore_epoch=None, **base))
    db.flush()
    db.add(LeaveHistoricalEvidencePreparation(id='4' * 64, restore_epoch='', **base))
    with pytest.raises(Exception):
        with db.begin_nested():
            db.flush()
    # and a pre-restore (NULL) proposal blocks an identical '' proposal: same bucket proven


def test_cross_replay_and_dual_role_root_refuse(pair):
    """(a) replay naming a DIFFERENT old selection refuses even with the correct stored
    digest; (b) a forged reverse link making the applied root also carry a rebind receipt
    fails verification closed."""
    from tests.test_restore_epoch_rebind import rebind
    db, e, kinds, old, auth = pair
    new, auth2 = _restored(db, e, auth)
    review, receipt = rebind(db, old.id, new.id, auth2)
    with pytest.raises(ValueError):
        apply_opening_rebind(db, old_selection_id='e' * 64, new_selection_id=new.id,
                             reviewed_rebind_sha256=review['rebind_sha256'],
                             reason='x', human_confirmed=True, **auth2)
    row = db.scalar(select(AuditLog).where(AuditLog.action == 'leave_opening_rebound'))
    body = json.loads(row.new_value)
    forged = {'old_selection_id': new.id, 'old_epoch': body['new_epoch'], 'new_epoch': 'E3-forged',
              'employee_id': body['employee_id'], 'amounts': body['amounts'],
              'effective_start': body['effective_start'], 'old_apply_sha256': body['old_apply_sha256'],
              'old_source_sha256': body['new_source_sha256'], 'new_source_sha256': 'c' * 64,
              'policy_sha256': body['policy_sha256']}
    db.add(AuditLog(actor='forged', action='leave_opening_rebound',
                    entity_type='leave_opening_selection', entity_id=old.id,
                    old_value=new.id, new_value=_canonical(forged), reason='forged reverse link'))
    db.flush()
    db.expire_all()
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=new.id, **auth2)
