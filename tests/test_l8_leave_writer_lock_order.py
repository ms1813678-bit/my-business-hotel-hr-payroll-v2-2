"""Real-kernel interleave: year attribution vs exact adjustment must serialize, never deadlock.

Both public kernels run concurrently on committed synthetic data in an isolated migrated schema.
Before the lock-order fix every attribution lost with DeadlockDetected (60 of 60 in a local run).
"""
import threading
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.leave_exact_adjustment import ExactAdjustment, adjustment_evidence_digest, apply_reviewed_exact_adjustment
from app.leave_year_attribution_store import apply_year_attribution, review_year_attribution
from app.models import AdminAccount, AdminSession, LeaveBalanceTransaction
from app.restore_lock import read_restore_epoch
from tests.test_leave_precision_migration import migrate, precision_schema, seed_legacy  # noqa: F401

ROUNDS = 12


def test_attribution_and_adjustment_interleave_without_deadlock(precision_schema):  # noqa: F811
    with precision_schema.begin() as connection:
        migrate(connection, 'head')
        seed_legacy(connection, Decimal('2.00'))
        employee_id, type_id = connection.execute(text(
            'SELECT employee_id,leave_type_id FROM leave_balance_transactions')).one()
    with Session(precision_schema) as db, db.begin():
        actor = AdminAccount(username='synthetic-l8-lock-order', password_hash='not-a-login',
                             role='system_admin', active=True, session_version=0)
        db.add(actor); db.flush()
        db.add(AdminSession(id='synthetic-l8-lock-session', admin_account_id=actor.id))
        ledger = [LeaveBalanceTransaction(employee_id=employee_id, leave_type_id=type_id,
            transaction_type='opening', days_delta=Decimal(10), reason=f'Synthetic {i}', created_by='synthetic')
            for i in range(ROUNDS)]
        db.add_all(ledger); db.flush()
        ledger_ids = [row.id for row in ledger]
        auth = dict(actor_id=actor.id, session_id='synthetic-l8-lock-session', session_version=0,
                    expected_epoch=read_restore_epoch(db))
    failures, done = [], []

    def attribute(ledger_id, barrier, index):
        try:
            with Session(precision_schema) as db:
                args = dict(ledger_id=ledger_id, allocations=((2025, Decimal(3)), (2026, Decimal(7))),
                            reviewed_on=date(2026, 11, 25), reason='Synthetic lock-order', **auth)
                review = review_year_attribution(db, **args); db.rollback()
                barrier.wait()
                apply_year_attribution(db, operation_id=f'{index + 1:064x}', reviewed_digest=review['reviewed_digest'],
                                       human_confirmed=True, **args)
                db.commit()
            done.append('attribution')
        except Exception as exc:  # noqa: BLE001
            failures.append(('attribution', type(exc).__name__, str(exc)[:120]))

    def adjust(proposal, kwargs, barrier):
        try:
            barrier.wait()
            apply_reviewed_exact_adjustment(precision_schema, proposal=proposal, **kwargs)
            done.append('adjustment')
        except Exception as exc:  # noqa: BLE001
            failures.append(('adjustment', type(exc).__name__, str(exc)[:120]))

    for index, ledger_id in enumerate(ledger_ids):
        proposal = ExactAdjustment(employee_id=employee_id, leave_type_id=type_id, days_delta=Decimal('-1.5'),
            reason='Synthetic adjustment', operation_key=f'synthetic-l8-op-{index}', earning_year=2025,
            released_on=date(2026, 2, 1))
        with Session(precision_schema) as db:
            digest = adjustment_evidence_digest(db, proposal)
        kwargs = dict(actor_id=auth['actor_id'], session_id=auth['session_id'],
                      session_version=0, reviewed_digest=digest)
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=attribute, args=(ledger_id, barrier, index)),
                   threading.Thread(target=adjust, args=(proposal, kwargs, barrier))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
    assert failures == []
    assert done.count('attribution') == ROUNDS and done.count('adjustment') == ROUNDS
