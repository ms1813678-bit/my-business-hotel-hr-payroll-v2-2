"""Real two-session PG race: zero vs carry (and zero vs zero replay) for one employee/type/year.
Commits to the scratch DB, so it runs only against a database whose name contains 'race' or 'test'.
Each case needs a fresh DB (committed synthetic rows); see RACE_FRESH_DB hint in the report."""
import threading
import time
from datetime import date

import pytest
from sqlalchemy import func, select, text

from app.db import SessionLocal, engine
from app.leave_carry_allocation_store import record_carry_allocation, review_carry_allocation
from app.leave_zero_decision import record_zero_decision, review_zero_decision
from app.models import AdminAccount, AdminSession, LeaveBalanceTransaction, LeaveCarryAllocation, LeaveZeroDecision
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_zero_decision import zero

WIN = dict(available_from=date(2026, 1, 1), available_through=date(2026, 4, 30))


def second_identity(db, args):
    acc = AdminAccount(username='race-second', password_hash='not-a-real-login', role='system_admin', active=True)
    db.add(acc); db.flush()
    ses = AdminSession(id='race-second-session', admin_account_id=acc.id)
    db.add(ses); db.flush()
    return {**args, 'actor_id': acc.id, 'session_id': ses.id, 'session_version': acc.session_version}


def run_pair(first, second, hold=1.5):
    """first() runs, holds its locks (no commit) for `hold`s; second() starts meanwhile and must block."""
    out = {}
    started = threading.Event()

    def a():
        with SessionLocal() as s:
            s.execute(text("SET lock_timeout='20s'"))
            try:
                out['a'] = ('ok', first(s))
                started.set()
                time.sleep(hold)
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback(); out['a'] = ('err', type(exc).__name__, str(exc)[:80]); started.set()

    def b():
        started.wait(10)
        with SessionLocal() as s:
            s.execute(text("SET lock_timeout='20s'"))
            t0 = time.time()
            try:
                out['b'] = ('ok', second(s))
                s.commit()
            except Exception as exc:  # noqa: BLE001
                s.rollback(); out['b'] = ('err', type(exc).__name__, str(exc)[:80])
            out['b_wait'] = time.time() - t0
    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start(); tb.start(); ta.join(60); tb.join(60)
    assert not ta.is_alive() and not tb.is_alive(), 'DEADLOCK/hang'
    return out


def counts():
    with SessionLocal() as s:
        return (s.scalar(select(func.count()).select_from(LeaveZeroDecision)),
                s.scalar(select(func.count()).select_from(LeaveCarryAllocation)),
                s.scalar(select(func.count()).select_from(LeaveBalanceTransaction)))


def prep(prior_year):
    db, e, kinds, args = prior_year
    assert 'test' in engine.url.database or 'race' in engine.url.database
    args2 = second_identity(db, args)
    _r, zc = zero(db, args, op='a')
    csel = dict(source_selection=args2, reason='Synthetic', **WIN)
    rv = review_carry_allocation(db, **csel)
    cc = dict(operation_id='c' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, **csel)
    base = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    db.commit()
    return base, zc, cc


@pytest.mark.parametrize('order', ['zero_first', 'carry_first'])
def test_zero_vs_carry_two_sessions_exactly_one_wins(prior_year, order):
    base, zc, cc = prep(prior_year)
    zf = lambda s: record_zero_decision(s, **zc)
    cf = lambda s: record_carry_allocation(s, **cc)
    out = run_pair(zf, cf) if order == 'zero_first' else run_pair(cf, zf)
    z, c, led = counts()
    assert out['a'][0] == 'ok', out
    assert out['b'][0] == 'err' and out['b'][1] == 'ValueError', out   # loser: clean refusal, not deadlock/serialization error
    assert out['b_wait'] >= 1.0, out                                   # loser really blocked behind the winner's locks
    assert (z, c) == ((1, 0) if order == 'zero_first' else (0, 1)), (z, c)
    assert led == base + (1 if order == 'zero_first' else 0), (led, base)  # zero debits once; carry adds no ledger row at record


def test_same_zero_operation_two_sessions_is_idempotent(prior_year):
    base, zc, _cc = prep(prior_year)
    zf = lambda s: record_zero_decision(s, **zc)
    out = run_pair(zf, zf)
    z, c, led = counts()
    assert out['a'][0] == 'ok' and out['b'][0] == 'ok' and out['a'][1] == out['b'][1], out
    assert (z, c, led) == (1, 0, base + 1)
