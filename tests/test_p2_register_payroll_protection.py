"""P2 candidate regressions: day-register protection and review invalidation.

No new financial behavior: register codes are not mapped to absence days. These
only prove (1) approved/closed payroll protects register preparation and
(2) a register change after absence/leave review forces a fresh review.
Synthetic data only. Written but NOT executed by the author (needs migrated PG).
"""
from datetime import date
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.leave_register_preparation import save_reviewed_register_preparation
from app.leave_register_review import prepare_register_edit
from app.models import LeaveRegisterEntry, PayrollRun
from tests.test_leave_register_preparation import preparation_store  # noqa: F401
from tests.test_leave_exact_adjustment import adjustment_store  # noqa: F401
from tests.test_leave_precision_migration import precision_schema  # noqa: F401
from tests.test_payroll_guards import (  # noqa: F401
    guard_client, login, create_run, set_absence, add_leave, approve_run, csrf)


@pytest.mark.parametrize('status', ['approved', 'closed'])
def test_register_preparation_refused_under_approved_or_closed_payroll(preparation_store, status):  # noqa: F811
    engine, edit, kwargs = preparation_store
    with Session(engine) as db, db.begin():
        db.add(PayrollRun(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
                          pay_date=date(2026, 2, 5), status=status, created_by='synthetic'))
    with pytest.raises(ValueError, match='payroll'):
        save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    with Session(engine) as db:
        assert db.scalar(select(LeaveRegisterEntry.id).where(
            LeaveRegisterEntry.employee_id == edit.employee_id)) is None


@pytest.mark.parametrize('status', ['draft', 'reopened'])
def test_register_preparation_allowed_under_draft_or_reopened_payroll(preparation_store, status):  # noqa: F811
    engine, edit, kwargs = preparation_store
    with Session(engine) as db, db.begin():
        db.add(PayrollRun(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
                          pay_date=date(2026, 2, 5), status=status, created_by='synthetic'))
    _id, rev = save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    assert rev == 1


def test_other_month_payroll_does_not_block_register_preparation(preparation_store):  # noqa: F811
    engine, edit, kwargs = preparation_store
    with Session(engine) as db, db.begin():
        db.add(PayrollRun(period_start=date(2026, 2, 1), period_end=date(2026, 2, 28),
                          pay_date=date(2026, 3, 5), status='closed', created_by='synthetic'))
    assert save_reviewed_register_preparation(engine, edit=edit, **kwargs)[1] == 1


def _review(client, item_id):
    token = csrf(client.get(f"/payroll/runs/{client.test_run_id}"))
    r = client.post(f"/payroll/items/{item_id}/absence-leave-review",
                    data={"csrf_token": token}, follow_redirects=False)
    assert "error" not in r.headers["location"]


def _register(client, work_date, code, revision=1):
    with client.test_session_factory() as db:
        row = db.scalar(select(LeaveRegisterEntry).where(
            LeaveRegisterEntry.employee_id == client.test_employee_id,
            LeaveRegisterEntry.work_date == work_date))
        if row is None:
            db.add(LeaveRegisterEntry(employee_id=client.test_employee_id, work_date=work_date,
                                      code=code, source='synthetic', revision=1, recorded_by='guard-admin'))
        else:
            row.code = code
            row.revision = revision
        db.commit()


def test_register_change_after_review_invalidates_confirmation(guard_client):  # noqa: F811
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    _register(client, date(2026, 10, 10), 'A')
    _review(client, item_id)
    _register(client, date(2026, 10, 10), '', revision=2)   # register corrected after review
    blocked = approve_run(client, run_id)
    assert blocked.headers["location"].endswith("error=absence_leave_conflict")
    _review(client, item_id)
    approved = approve_run(client, run_id)
    assert "error" not in approved.headers["location"]


def test_register_unchanged_after_review_keeps_confirmation(guard_client):  # noqa: F811
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    _register(client, date(2026, 10, 10), 'A')
    _review(client, item_id)
    approved = approve_run(client, run_id)
    assert "error" not in approved.headers["location"]


def test_register_alone_never_creates_a_conflict_or_changes_absence(guard_client):  # noqa: F811
    """Legacy behavior preserved: no register-to-absence mapping, no new blocker."""
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    _register(client, date(2026, 10, 10), 'A')
    approved = approve_run(client, run_id)
    assert "error" not in approved.headers["location"]
    with client.test_session_factory() as db:
        assert db.get(PayrollRun, run_id).status == "approved"


def test_uncommitted_payroll_approval_blocks_then_refuses(preparation_store):  # noqa: F811
    engine, edit, kwargs = preparation_store
    with Session(engine) as db, db.begin():
        db.add(PayrollRun(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31),
                          pay_date=date(2026, 2, 5), status='draft', created_by='s'))
    a = Session(engine)
    a.begin()
    run = a.scalar(select(PayrollRun).with_for_update())   # what the approve route does
    run.status = 'approved'
    a.flush()
    try:
        with pytest.raises(OperationalError):  # waits on the run row, lock timeout, never a silent write
            save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    finally:
        a.commit()
        a.close()
    with pytest.raises(ValueError, match='payroll'):
        save_reviewed_register_preparation(engine, edit=edit, **kwargs)
    with Session(engine) as db:
        assert db.scalar(select(LeaveRegisterEntry.id)) is None


def _wait_blocked(engine, needle, seconds=10):
    import time
    from sqlalchemy import text
    end = time.time() + seconds
    while time.time() < end:
        with engine.connect() as c:
            n = c.execute(text("select count(*) from pg_locks l join pg_stat_activity a on a.pid=l.pid "
                               "where not l.granted and a.query ilike :q"), {'q': f'%{needle}%'}).scalar()
        if n:
            return True
        time.sleep(0.05)
    return False


def test_forced_interleave_approve_scope_vs_register_preparation_no_deadlock(preparation_store):  # noqa: F811
    """Real lock_scope() (approve/close) vs real preparation, forced ordering.

    A holds payroll_items+payroll_runs and waits at payroll_payments (held by C);
    B then runs preparation; C releases. A table lock on payroll_runs inside
    preparation would deadlock here (A needs leave_month_close, B holds it).
    """
    import threading
    from sqlalchemy import text
    from app.overtime_day_award import lock_scope
    engine, edit, kwargs = preparation_store
    out = {}
    c = engine.connect(); ct = c.begin()
    c.execute(text('LOCK TABLE payroll_payments IN ACCESS EXCLUSIVE MODE'))

    def approve_side():
        try:
            with Session(engine) as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout = '8s'"))
                lock_scope(db)
                out['A'] = 'ok'
        except Exception as exc:
            out['A'] = type(exc).__name__ + ':' + str(exc)[:60]

    def prep_side():
        try:
            out['B'] = save_reviewed_register_preparation(engine, edit=edit, **kwargs)
        except Exception as exc:
            out['B'] = type(exc).__name__ + ':' + str(exc)[:60]

    ta = threading.Thread(target=approve_side); ta.start()
    assert _wait_blocked(engine, 'payroll_items')          # A queued behind C, already holding payroll_runs
    tb = threading.Thread(target=prep_side); tb.start()
    import time; time.sleep(1.5)                            # B has taken its register/month-close locks
    ct.rollback(); c.close()                                # release C
    ta.join(20); tb.join(20)
    assert not ta.is_alive() and not tb.is_alive()
    assert out['A'] == 'ok', out
    assert isinstance(out['B'], tuple), out                 # no deadlock, preparation completed


def test_forced_interleave_real_approve_scope_vs_real_return_register_is_gate_serialized(monkeypatch):
    """Real monthly_lock_scope() vs real consume_return_register(), pinned interleave.

    Their LOCK TABLE lists cross-order payroll_runs/leave_month_close, but the
    restore gate (shared advisory in monthly_lock_scope, exclusive advisory in
    the consume guard) serializes them first, so no deadlock occurs. Identity
    validation is stubbed (needs a committed admin row the sole-admin trigger
    forbids deleting); the real gate acquisition and the real LOCK TABLE run.
    """
    import threading, time
    from types import SimpleNamespace
    from sqlalchemy import text
    from app.db import SessionLocal, engine as app_engine
    from app.overtime_day_award import monthly_lock_scope
    import app.leave_return_register as rr
    from app.restore_lock import acquire_credential_write_lock

    def gate_guard(db, **identity):
        acquire_credential_write_lock(db)
        return SimpleNamespace(id=1)
    monkeypatch.setattr(rr, '_guard', gate_guard)
    out = {}
    c = app_engine.connect(); ct = c.begin()
    c.execute(text('LOCK TABLE payroll_payments IN ACCESS EXCLUSIVE MODE'))

    def approve_side():
        try:
            with SessionLocal() as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout = '2500ms'"))
                monthly_lock_scope(db)
                out['A'] = 'ok'
        except Exception as exc:
            out['A'] = type(exc).__name__ + ':' + str(exc)[:80]

    def register_side():
        try:
            with SessionLocal() as db, db.begin():
                db.execute(text("SET LOCAL lock_timeout = '2500ms'"))
                rr.consume_return_register(db, operation_id='a' * 64, comparison_id='b' * 64, employee_id=1,
                                           reviewed_digest='c' * 64, human_confirmed=True, reason='gate proof',
                                           actor_id=1, session_id='s', session_version=0, expected_epoch=None)
                out['B'] = 'ok'
        except Exception as exc:
            out['B'] = type(exc).__name__ + ':' + str(exc)[:80]

    ta = threading.Thread(target=approve_side); ta.start()
    time.sleep(0.5)                      # A holds the shared gate and its early table locks, queued behind C
    tb = threading.Thread(target=register_side); tb.start()
    time.sleep(0.8)                      # B must be queued behind A's gate, holding no table lock
    ct.rollback(); c.close()
    ta.join(30); tb.join(30)
    assert not ta.is_alive() and not tb.is_alive()
    assert out['A'] == 'ok', out
    assert 'Deadlock' not in out['B'] and 'LockNotAvailable' not in out['B'], out
